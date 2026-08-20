from __future__ import annotations

import unittest

from channel_connections.ports import ProviderUnavailable
from channel_data.models import CollectionKind, CollectionStatus
from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import EnqueueRun, ExecuteRun, RunFailureReason, RunKind, RunStatus
from collection_jobs.tests.support import build_stack, context


class OwnerContentFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)

    def run_owner_content(self, *, key: str = "content-1"):
        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key=key,
            ),
        )
        return self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=queued.run_id, idempotency_key=f"exec-{key}")
        )

    def run_subscribers(self, *, key: str = "subs-1"):
        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key=key,
            ),
        )
        return self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=queued.run_id, idempotency_key=f"exec-{key}")
        )

    def states(self, kind: CollectionKind):
        history = self.stack.channel_data.list_collection_history(
            self.owner,
            __import__(
                "channel_data.models", fromlist=["CollectionHistoryQuery"]
            ).CollectionHistoryQuery(channel_id="UC_channel_1"),
        )
        return [state for state in history.items if state.kind is kind]


class OwnerContentExecutionTests(OwnerContentFixture):
    def test_a_run_publishes_the_inventory_then_covers_every_video(self) -> None:
        finished = self.run_owner_content()

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        self.assertIsNone(finished.failure_reason)
        videos = self.states(CollectionKind.VIDEOS)
        comments = self.states(CollectionKind.COMMENTS)
        self.assertEqual([state.status for state in videos], [CollectionStatus.COMPLETE])
        self.assertEqual([state.status for state in comments], [CollectionStatus.COMPLETE])

    def test_pages_and_quota_cover_both_phases(self) -> None:
        finished = self.run_owner_content()

        video_pages = 2
        comment_pages = len(self.stack.data_gateway.videos)
        self.assertGreaterEqual(finished.pages_fetched, video_pages + comment_pages)
        self.assertEqual(
            finished.quota_spent,
            finished.pages_fetched * self.stack.data_gateway.QUOTA_COST,
        )

    def test_videos_without_comments_are_still_covered(self) -> None:
        self.run_subscribers()
        self.run_owner_content()

        dataset = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )

        self.assertEqual(
            {row.author_channel_id for row in dataset.author_activity},
            {"UC_sub_1", "UC_sub_2"},
        )
        self.assertIsNotNone(dataset.inventory_id)

    def test_stored_activity_has_no_text_or_display_name(self) -> None:
        self.run_subscribers()
        self.run_owner_content()

        dataset = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )
        fields = set(type(dataset.author_activity[0]).__slots__)

        self.assertEqual(fields, {"author_channel_id", "comment_count", "last_comment_at"})

    def test_a_failure_during_coverage_promotes_nothing(self) -> None:
        self.run_subscribers()
        self.stack.data_gateway.failure = ProviderUnavailable()

        finished = self.run_owner_content()

        self.assertIn(finished.status, {RunStatus.QUEUED, RunStatus.FAILED})
        with self.assertRaises(Exception) as raised:
            self.stack.channel_data.load_silent_analysis_dataset(self.owner, "UC_channel_1")
        self.assertEqual(getattr(raised.exception, "code", None), "DATASET_NOT_READY")

    def test_a_quota_stop_during_coverage_keeps_prior_data_readable(self) -> None:
        self.run_subscribers()
        self.run_owner_content()
        before = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )

        limited = build_stack(daily_quota_units=6)
        owner = context()
        connection = limited.connect(owner)
        queued = limited.jobs.enqueue_run(
            owner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key="c1",
            ),
        )
        finished = limited.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x1")
        )

        self.assertEqual(finished.status, RunStatus.PARTIAL)
        self.assertEqual(finished.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
        after = self.stack.channel_data.load_silent_analysis_dataset(
            self.owner, "UC_channel_1"
        )
        self.assertEqual(after.inventory_id, before.inventory_id)

    def test_owner_content_and_subscriber_runs_are_independent(self) -> None:
        content = self.run_owner_content()
        subscribers = self.run_subscribers()

        self.assertEqual(content.kind, RunKind.OWNER_CONTENT)
        self.assertEqual(subscribers.kind, RunKind.SUBSCRIBERS)
        self.assertEqual(subscribers.status, RunStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
