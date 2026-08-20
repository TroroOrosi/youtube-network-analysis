from __future__ import annotations

import unittest
from datetime import timedelta

from collection_jobs.models import EnqueueRun, ExecuteRun, RunKind, RunStatus
from collection_jobs.tests.support import NOW, build_stack, context
from subscriber_analytics.analytics_core import (
    AnalysisRequest,
    CommentActivity,
    Segment,
    SubscriberRecord,
    analyze,
)
from workspace_access.models import (
    CreateWorkspace,
    Permission,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceSelection,
)
from workspace_access.service import WorkspaceAccessService


class EndToEndTests(unittest.TestCase):
    """Provider rows to accepted channel data to analytics segments."""

    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)

    def run_kind(self, kind: RunKind, *, key: str) -> None:
        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=kind,
                idempotency_key=key,
            ),
        )
        finished = self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=queued.run_id, idempotency_key=f"x-{key}")
        )
        self.assertEqual(finished.status, RunStatus.SUCCEEDED)

    def test_collected_rows_classify_into_all_four_segments(self) -> None:
        self.run_kind(RunKind.SUBSCRIBERS, key="subs")
        self.run_kind(RunKind.OWNER_CONTENT, key="content")

        dataset = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )
        result = analyze(
            tuple(
                SubscriberRecord(
                    channel_id=entry.subscriber_channel_id,
                    title=entry.title,
                    api_published_at=entry.api_published_at,
                    first_seen_at=entry.first_seen_at,
                    last_seen_at=entry.last_seen_at,
                )
                for entry in dataset.subscriber_registry
            ),
            tuple(
                CommentActivity(
                    author_channel_id=row.author_channel_id,
                    comment_count=row.comment_count,
                    last_comment_at=row.last_comment_at,
                )
                for row in dataset.author_activity
            ),
            AnalysisRequest(reference_time=NOW),
        )

        segments = {row.segment for row in result.rows}
        self.assertEqual(
            segments,
            {Segment.NEW_SILENT, Segment.OLD_SILENT, Segment.DORMANT, Segment.ACTIVE},
        )

    def test_a_real_workspace_context_drives_the_whole_pipeline(self) -> None:
        access = WorkspaceAccessService()
        issued = access.establish_session(
            VerifiedIdentity(
                issuer="https://accounts.example",
                subject="owner-subject",
                authenticated_at=NOW,
            )
        )
        session = access.authenticate_session(SessionEvidence(secret=issued.secret))
        created = access.create_workspace(session, CreateWorkspace(name="運用ワークスペース"))
        manage = access.resolve_workspace_context(
            session,
            WorkspaceSelection(workspace_id=created.workspace_id),
            Permission.CHANNEL_MANAGE_CONNECTION,
        )
        runner = access.resolve_workspace_context(
            session,
            WorkspaceSelection(workspace_id=created.workspace_id),
            Permission.COLLECTION_RUN,
        )

        connection = self.stack.connect(manage, key="real-connect")
        queued = self.stack.jobs.enqueue_run(
            runner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="real-1",
            ),
        )
        finished = self.stack.jobs.execute_run(
            runner, ExecuteRun(run_id=queued.run_id, idempotency_key="real-x")
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        self.assertEqual(finished.workspace_id, created.workspace_id)

    def test_the_jobs_module_never_touches_credentials_or_the_network(self) -> None:
        import pathlib

        import collection_jobs.memory as memory
        import collection_jobs.models as models
        import collection_jobs.service as service

        for module in (memory, models, service):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for forbidden in (
                "token.json",
                "client_secret",
                "requests",
                "urllib",
                "RedactedSecret",
                "CredentialVault",
            ):
                self.assertNotIn(forbidden, source)

    def test_a_second_owner_content_run_refreshes_the_dataset(self) -> None:
        self.run_kind(RunKind.SUBSCRIBERS, key="subs")
        self.run_kind(RunKind.OWNER_CONTENT, key="content")
        first = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )

        self.stack.clock.advance(timedelta(days=1))
        self.run_kind(RunKind.OWNER_CONTENT, key="content-2")
        second = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )

        self.assertNotEqual(first.inventory_id, second.inventory_id)
        self.assertEqual(
            {row.author_channel_id for row in first.author_activity},
            {row.author_channel_id for row in second.author_activity},
        )


if __name__ == "__main__":
    unittest.main()
