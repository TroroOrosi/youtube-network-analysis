from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from channel_connections.ports import ProviderUnavailable
from channel_data.models import CollectionKind, CollectionStatus
from collection_jobs.models import EnqueueRun, ExecuteRun, RunFailureReason, RunKind, RunStatus
from collection_jobs.tests.support import NOW, TickingClock, build_stack, context


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

    def test_a_quota_stop_during_coverage_queues_the_run_for_tomorrow(self) -> None:
        """Out of units is not a failure any more: it is a run waiting for midnight.

        The budget is spent for the UTC day, so there is nothing this run can
        do until the ledger rolls over. It keeps what it covered, holds the
        collection open, and names the first moment it can afford to continue.
        """

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

        stopped = limited.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x1")
        )

        self.assertEqual(stopped.status, RunStatus.QUEUED)
        self.assertEqual(stopped.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
        self.assertEqual(
            stopped.next_attempt_at, datetime(2026, 8, 22, tzinfo=UTC)
        )

    def test_a_run_out_of_units_continues_the_next_day_without_refetching(self) -> None:
        """The whole point of suspending: yesterday's calls are not spent again.

        A collection larger than one day of units can only ever finish if the
        second day starts where the first stopped. The gateway records every
        call it was asked for, so covering a video twice would show up here.
        """

        limited = build_stack(daily_quota_units=12)
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
        stopped = limited.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x1")
        )
        self.assertEqual(stopped.status, RunStatus.QUEUED)

        limited.clock.advance(timedelta(days=1))
        finished = limited.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x2")
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        covered = [
            call.video_id
            for call in limited.data_gateway.calls
            if call.operation == "LIST_VIDEO_COMMENT_AUTHORS"
        ]
        self.assertEqual(sorted(set(covered)), ["video-1", "video-2", "video-3"])
        self.assertEqual(len(covered), len(set(covered)))

    def test_a_slice_that_runs_out_of_time_is_continued_by_the_next_call(self) -> None:
        """What a browser does: many short calls, one run, nothing lost between them."""

        sliced = build_stack(jobs_clock=TickingClock(NOW, timedelta(seconds=1)))
        owner = context()
        connection = sliced.connect(owner)
        queued = sliced.jobs.enqueue_run(
            owner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key="c1",
            ),
        )

        statuses = []
        run = queued
        for index in range(12):
            run = sliced.jobs.execute_run(
                owner,
                ExecuteRun(
                    run_id=queued.run_id,
                    idempotency_key=f"slice-{index}",
                    slice_seconds=4,
                ),
            )
            statuses.append(run.status)
            if run.status is not RunStatus.QUEUED:
                break

        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        self.assertGreater(len(statuses), 1)
        covered = [
            call.video_id
            for call in sliced.data_gateway.calls
            if call.operation == "LIST_VIDEO_COMMENT_AUTHORS"
        ]
        self.assertEqual(sorted(set(covered)), ["video-1", "video-2", "video-3"])
        self.assertEqual(len(covered), len(set(covered)))

    def test_a_suspended_run_leaves_the_accepted_dataset_alone(self) -> None:
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
        limited.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x1")
        )

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


    def test_budget_starvation_beyond_three_days_retains_and_then_finishes_comments(self) -> None:
        """A daily budget is a wait condition even when other work used it first."""
        limited = build_stack(daily_quota_units=6)
        owner = context()
        connection = limited.connect(owner)
        content = limited.jobs.enqueue_run(owner, EnqueueRun(
            connection_id=connection.connection_id, kind=RunKind.OWNER_CONTENT,
            idempotency_key="c1"))
        stopped = limited.jobs.execute_run(owner, ExecuteRun(
            run_id=content.run_id, idempotency_key="x0"))
        self.assertEqual(stopped.status, RunStatus.QUEUED)
        # Exactly two pages complete the burner instead of leaving another
        # unfinished subscriber run which would correctly block its replacement.
        limited.data_gateway.subscribers = limited.data_gateway.subscribers[:4]
        for day in range(1, 6):
            limited.clock.advance(timedelta(days=1))
            burner = limited.jobs.enqueue_run(owner, EnqueueRun(
                connection_id=connection.connection_id, kind=RunKind.SUBSCRIBERS,
                idempotency_key=f"s{day}"))
            self.assertEqual(limited.jobs.execute_run(owner, ExecuteRun(
                run_id=burner.run_id, idempotency_key=f"b{day}")).status, RunStatus.SUCCEEDED)
            stopped = limited.jobs.execute_run(owner, ExecuteRun(
                run_id=content.run_id, idempotency_key=f"x{day}"))
            self.assertEqual(stopped.status, RunStatus.QUEUED)
            self.assertEqual(stopped.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
            self.assertIsNotNone(stopped.next_attempt_at)
            self.assertEqual(stopped.attempt, 1)
        # With the next days' budgets available, finish without relisting videos.
        for day in range(6, 12):
            limited.clock.advance(timedelta(days=1))
            stopped = limited.jobs.execute_run(owner, ExecuteRun(
                run_id=content.run_id, idempotency_key=f"x{day}"))
            if stopped.status is not RunStatus.QUEUED:
                break
        self.assertEqual(stopped.status, RunStatus.SUCCEEDED)
        videos = [c for c in limited.data_gateway.calls if c.operation == "LIST_VIDEOS"]
        self.assertEqual(len(videos), 2)

    def test_a_day_that_covers_something_clears_the_stall_count(self) -> None:
        """Slow is not stalled: any video covered means the run is still moving."""

        limited = build_stack(daily_quota_units=12)
        owner = context()
        connection = limited.connect(owner)
        content = limited.jobs.enqueue_run(
            owner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key="c1",
            ),
        )
        run = limited.jobs.execute_run(
            owner, ExecuteRun(run_id=content.run_id, idempotency_key="x0")
        )

        for day in range(1, 6):
            if run.status is not RunStatus.QUEUED:
                break
            limited.clock.advance(timedelta(days=1))
            run = limited.jobs.execute_run(
                owner, ExecuteRun(run_id=content.run_id, idempotency_key=f"x{day}")
            )

        self.assertEqual(run.status, RunStatus.SUCCEEDED)


    def test_audience_network_run_uses_comment_authors_and_persists_public_edges(self) -> None:
        from channel_connections import models as connection_models

        network_kind = getattr(RunKind, "AUDIENCE_NETWORK", None)
        row_type = getattr(connection_models, "ChannelSubscriptionRow", None)
        self.assertIsNotNone(network_kind)
        self.assertIsNotNone(row_type)

        self.run_subscribers()
        self.run_owner_content()
        self.stack.data_gateway.channel_subscriptions = {
            "UC_sub_1": (
                row_type("UC_other_a", "登録先A"),
                row_type("UC_shared", "共通チャンネル"),
            ),
            "UC_sub_2": (
                row_type("UC_other_b", "登録先B"),
                row_type("UC_shared", "共通チャンネル"),
            ),
        }

        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=network_kind,
                idempotency_key="network-1",
            ),
        )
        finished = self.stack.jobs.execute_run(
            self.owner,
            ExecuteRun(run_id=queued.run_id, idempotency_key="network-exec-1"),
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        snapshot = self.stack.jobs.latest_audience_network(
            self.owner, "UC_channel_1"
        )
        self.assertEqual(snapshot.run_id, queued.run_id)
        self.assertEqual(len(snapshot.viewers), 2)
        self.assertEqual(
            {row.channel_id for viewer in snapshot.viewers for row in viewer.subscriptions},
            {"UC_other_a", "UC_other_b", "UC_shared"},
        )

    def test_private_comment_author_is_skipped_without_failing_network_run(self) -> None:
        from channel_connections import models as connection_models

        network_kind = getattr(RunKind, "AUDIENCE_NETWORK", None)
        row_type = getattr(connection_models, "ChannelSubscriptionRow", None)
        self.assertIsNotNone(network_kind)
        self.assertIsNotNone(row_type)

        self.run_subscribers()
        self.run_owner_content()
        self.stack.data_gateway.channel_subscriptions = {
            "UC_sub_1": (row_type("UC_shared", "共通チャンネル"),),
            "UC_sub_2": None,
        }

        queued = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=network_kind,
                idempotency_key="network-private",
            ),
        )
        finished = self.stack.jobs.execute_run(
            self.owner,
            ExecuteRun(run_id=queued.run_id, idempotency_key="network-private-exec"),
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        snapshot = self.stack.jobs.latest_audience_network(
            self.owner, "UC_channel_1"
        )
        public = [viewer for viewer in snapshot.viewers if viewer.public]
        private = [viewer for viewer in snapshot.viewers if not viewer.public]
        self.assertEqual(len(public), 1)
        self.assertEqual(len(private), 1)


if __name__ == "__main__":
    unittest.main()
