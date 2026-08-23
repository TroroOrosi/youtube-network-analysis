from __future__ import annotations

import unittest
from datetime import timedelta

from analysis_api.errors import AnalysisApiError
from analysis_api.models import (
    AnalysisFilterInput,
    AnalysisPageRequest,
    CompareChannels,
    DeleteView,
    ExportAnalysis,
    RunAnalysis,
    SaveView,
)
from analysis_api.service import EXPORT_COLUMNS, AnalysisApiService
from channel_connections.tests.support import SequenceTokens
from collection_jobs.models import EnqueueRun, ExecuteRun, RunKind
from collection_jobs.tests.support import NOW, build_stack, context
from workspace_access.models import Permission


class MemoryDocument:
    def __init__(self) -> None:
        self.document: str | None = None

    def load(self) -> str | None:
        return self.document

    def save(self, document: str) -> None:
        self.document = document


class AnalysisFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)
        self.collect(RunKind.SUBSCRIBERS, "subs")
        self.collect(RunKind.OWNER_CONTENT, "content")
        self.analysis = AnalysisApiService(
            clock=self.stack.clock,
            tokens=SequenceTokens("analysis"),
            channel_data=self.stack.channel_data,
        )

    def collect(self, kind: RunKind, key: str) -> None:
        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=kind,
                idempotency_key=key,
            ),
        )
        self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=queued.run_id, idempotency_key=f"x-{key}")
        )


class RunAnalysisTests(AnalysisFixture):
    def test_analysis_summarizes_the_accepted_dataset(self) -> None:
        page = self.analysis.run_analysis(self.owner, RunAnalysis(channel_id="UC_channel_1"))

        self.assertEqual(page.summary.channel_id, "UC_channel_1")
        self.assertEqual(page.summary.reference_time, NOW)
        self.assertEqual(page.summary.scope_count, 5)
        self.assertEqual(page.summary.filtered_count, 5)
        self.assertEqual(len(page.rows), 5)
        self.assertEqual(
            {code.value for code in page.summary.limitations},
            {"PUBLIC_SUBSCRIPTIONS_ONLY", "PROVIDER_RESULT_CAP_POSSIBLE"},
        )

    def test_every_segment_is_counted(self) -> None:
        page = self.analysis.run_analysis(self.owner, RunAnalysis(channel_id="UC_channel_1"))

        counts = dict(page.summary.segment_counts)
        self.assertEqual(sum(counts.values()), 5)
        self.assertEqual(
            set(counts), {"ACTIVE", "DORMANT", "NEW_SILENT", "OLD_SILENT"}
        )

    def test_filters_are_delegated_to_analytics_core(self) -> None:
        page = self.analysis.run_analysis(
            self.owner,
            RunAnalysis(
                channel_id="UC_channel_1",
                filters=AnalysisFilterInput(never_commented=True),
            ),
        )

        self.assertTrue(all(row.comment_count == 0 for row in page.rows))
        self.assertEqual(page.summary.scope_count, 5)
        self.assertLess(page.summary.filtered_count, 5)

    def test_segment_allowlist_narrows_the_result(self) -> None:
        page = self.analysis.run_analysis(
            self.owner,
            RunAnalysis(
                channel_id="UC_channel_1",
                filters=AnalysisFilterInput(segments=("OLD_SILENT",)),
            ),
        )

        self.assertTrue(page.rows)
        self.assertTrue(all(row.segment == "OLD_SILENT" for row in page.rows))

    def test_analysis_requires_analysis_read(self) -> None:
        stranger = context("workspace-1", Permission.COLLECTION_READ)

        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.run_analysis(stranger, RunAnalysis(channel_id="UC_channel_1"))

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_a_channel_without_data_reports_the_exact_reason(self) -> None:
        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.run_analysis(self.owner, RunAnalysis(channel_id="UC_unknown"))

        self.assertEqual(raised.exception.code, "DATASET_NOT_READY")
        self.assertEqual(raised.exception.reason_code, "NO_SUBSCRIBER_SNAPSHOT")

    def test_a_foreign_workspace_sees_no_data(self) -> None:
        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.run_analysis(
                context("workspace-2"), RunAnalysis(channel_id="UC_channel_1")
            )

        self.assertEqual(raised.exception.code, "DATASET_NOT_READY")

    def test_rows_are_paged_with_single_use_cursors(self) -> None:
        first = self.analysis.run_analysis(
            self.owner,
            RunAnalysis(channel_id="UC_channel_1"),
            AnalysisPageRequest(limit=2),
        )
        self.assertIsNotNone(first.next_cursor)

        second = self.analysis.run_analysis(
            self.owner,
            RunAnalysis(channel_id="UC_channel_1"),
            AnalysisPageRequest(cursor=first.next_cursor, limit=2),
        )

        self.assertEqual(len(second.rows), 2)
        self.assertNotEqual(
            {row.subscriber_channel_id for row in first.rows},
            {row.subscriber_channel_id for row in second.rows},
        )
        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.run_analysis(
                self.owner,
                RunAnalysis(channel_id="UC_channel_1"),
                AnalysisPageRequest(cursor=first.next_cursor, limit=2),
            )
        self.assertEqual(raised.exception.code, "INVALID_CURSOR")

    def test_a_newer_generation_expires_the_cursor(self) -> None:
        page = self.analysis.run_analysis(
            self.owner,
            RunAnalysis(channel_id="UC_channel_1"),
            AnalysisPageRequest(limit=2),
        )
        self.stack.clock.advance(timedelta(days=1))
        self.collect(RunKind.SUBSCRIBERS, "subs-2")

        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.run_analysis(
                self.owner,
                RunAnalysis(channel_id="UC_channel_1"),
                AnalysisPageRequest(cursor=page.next_cursor, limit=2),
            )

        self.assertEqual(raised.exception.code, "CURSOR_EXPIRED")


class ComparisonTests(AnalysisFixture):
    def test_comparison_reports_ready_and_not_ready_channels(self) -> None:
        result = self.analysis.compare_channels(
            self.owner, CompareChannels(channel_ids=("UC_channel_1", "UC_missing"))
        )

        ready = {entry.channel_id: entry for entry in result.entries}
        self.assertIsNotNone(ready["UC_channel_1"].summary)
        self.assertIsNone(ready["UC_missing"].summary)
        self.assertEqual(ready["UC_missing"].not_ready_reason, "NO_SUBSCRIBER_SNAPSHOT")

    def test_comparison_rejects_more_than_five_channels(self) -> None:
        with self.assertRaises(AnalysisApiError) as raised:
            CompareChannels(channel_ids=tuple(f"UC_{i}" for i in range(6)))

        self.assertEqual(raised.exception.field, "channel_ids")


class ExportTests(AnalysisFixture):
    def test_export_produces_a_deterministic_csv(self) -> None:
        exporter = context(
            "workspace-1", Permission.ANALYSIS_READ, Permission.ANALYSIS_EXPORT
        )

        document = self.analysis.export_analysis(
            exporter, ExportAnalysis(channel_id="UC_channel_1")
        )

        self.assertTrue(document.filename.endswith(".csv"))
        self.assertTrue(document.content.startswith(b"\xef\xbb\xbf"))
        text = document.content.decode("utf-8-sig")
        self.assertEqual(text.splitlines()[0], ",".join(EXPORT_COLUMNS))
        self.assertEqual(document.row_count, 5)
        self.assertEqual(len(text.strip().splitlines()), 6)

    def test_export_requires_the_export_permission(self) -> None:
        reader = context("workspace-1", Permission.ANALYSIS_READ)

        with self.assertRaises(AnalysisApiError) as raised:
            self.analysis.export_analysis(reader, ExportAnalysis(channel_id="UC_channel_1"))

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")


class SavedViewTests(AnalysisFixture):
    def save(self, *, name: str = "沈黙のみ", key: str = "view-1"):
        return self.analysis.save_view(
            self.owner,
            SaveView(
                name=name,
                channel_id="UC_channel_1",
                filters=AnalysisFilterInput(never_commented=True),
                idempotency_key=key,
            ),
        )

    def test_a_view_is_saved_and_listed_for_the_workspace(self) -> None:
        view = self.save()

        self.assertEqual(view.workspace_id, "workspace-1")
        self.assertEqual(view.name, "沈黙のみ")
        self.assertEqual(
            [item.view_id for item in self.analysis.list_views(self.owner)],
            [view.view_id],
        )
        self.assertEqual(self.analysis.list_views(context("workspace-2")), ())

    def test_saved_views_survive_a_service_restart(self) -> None:
        document = MemoryDocument()
        first = AnalysisApiService(
            clock=self.stack.clock,
            tokens=SequenceTokens("first"),
            channel_data=self.stack.channel_data,
            state_store=document,
        )
        saved = first.save_view(
            self.owner,
            SaveView(
                name="再起動後も使う条件",
                channel_id="UC_channel_1",
                filters=AnalysisFilterInput(
                    subscribed_within_days=90,
                    never_commented=True,
                    segments=("OLD_SILENT",),
                ),
                idempotency_key="persistent-view",
            ),
        )

        restarted = AnalysisApiService(
            clock=self.stack.clock,
            tokens=SequenceTokens("second"),
            channel_data=self.stack.channel_data,
            state_store=document,
        )

        self.assertEqual(restarted.list_views(self.owner), (saved,))

    def test_saving_is_idempotent_and_conflicts_on_reuse(self) -> None:
        first = self.save()
        second = self.save()

        self.assertEqual(first, second)
        with self.assertRaises(AnalysisApiError) as raised:
            self.save(name="別の名前")
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_a_saved_view_reproduces_its_analysis(self) -> None:
        view = self.save()

        page = self.analysis.run_analysis(
            self.owner, RunAnalysis(channel_id=view.channel_id, filters=view.filters)
        )

        self.assertTrue(all(row.comment_count == 0 for row in page.rows))

    def test_a_foreign_or_missing_view_is_hidden(self) -> None:
        view = self.save()

        for actor, view_id in (
            (context("workspace-2"), view.view_id),
            (self.owner, "view_missing"),
        ):
            with self.assertRaises(AnalysisApiError) as raised:
                self.analysis.get_view(actor, view_id)
            self.assertEqual(raised.exception.code, "VIEW_NOT_FOUND_OR_FORBIDDEN")

    def test_deleting_a_view_is_idempotent(self) -> None:
        view = self.save()

        self.analysis.delete_view(
            self.owner, DeleteView(view_id=view.view_id, idempotency_key="d1")
        )
        self.analysis.delete_view(
            self.owner, DeleteView(view_id=view.view_id, idempotency_key="d1")
        )

        self.assertEqual(self.analysis.list_views(self.owner), ())

    def test_workspace_cascade_removes_only_its_own_views(self) -> None:
        self.save()
        other = context("workspace-2")
        self.analysis.save_view(
            other,
            SaveView(
                name="別ワークスペース",
                channel_id="UC_channel_1",
                filters=AnalysisFilterInput(),
                idempotency_key="view-1",
            ),
        )

        self.analysis.delete_workspace_views(self.owner)

        self.assertEqual(self.analysis.list_views(self.owner), ())
        self.assertEqual(len(self.analysis.list_views(other)), 1)


if __name__ == "__main__":
    unittest.main()
