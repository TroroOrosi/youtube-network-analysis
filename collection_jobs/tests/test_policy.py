from __future__ import annotations

import unittest
from datetime import timedelta

from channel_connections.models import ReportCredentialInvalidation
from channel_connections.ports import ProviderUnavailable
from channel_data.models import CollectionStatus
from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import (
    BACKOFF_SCHEDULE,
    CancelRun,
    EnqueueRun,
    ExecuteRun,
    MAX_ATTEMPTS,
    RunFailureReason,
    RunKind,
    RunStatus,
)
from collection_jobs.tests.support import NOW, build_stack, context


class PolicyFixture(unittest.TestCase):
    quota_units: int | None = None

    def setUp(self) -> None:
        self.stack = build_stack(daily_quota_units=self.quota_units)
        self.owner = context()
        self.connection = self.stack.connect(self.owner)
        self.run = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="enqueue-1",
            ),
        )

    def execute(self, *, key: str = "execute-1"):
        return self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=self.run.run_id, idempotency_key=key)
        )

    def collection_states(self):
        history = self.stack.channel_data.list_collection_history(
            self.owner,
            __import__(
                "channel_data.models", fromlist=["CollectionHistoryQuery"]
            ).CollectionHistoryQuery(channel_id="UC_channel_1"),
        )
        return list(history.items)


class QuotaTests(PolicyFixture):
    quota_units = 5

    def test_a_run_stops_before_an_unaffordable_call(self) -> None:
        finished = self.execute()

        self.assertEqual(finished.status, RunStatus.PARTIAL)
        self.assertEqual(finished.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
        self.assertEqual(finished.pages_fetched, 1)
        self.assertEqual(finished.quota_spent, 3)
        self.assertEqual(
            [state.status for state in self.collection_states()],
            [CollectionStatus.PARTIAL],
        )

    def test_a_partial_run_promotes_nothing(self) -> None:
        self.execute()

        freshness = self.stack.channel_data.get_freshness(self.owner, "UC_channel_1")
        self.assertIsNone(freshness.subscribers.latest_accepted_success)

    def test_quota_is_counted_per_workspace_and_day(self) -> None:
        self.execute()

        other = context("workspace-2")
        other_stack_connection = self.stack.connect(
            other, provider_channel_id="UC_channel_1", key="connect-2"
        )
        queued = self.stack.jobs.enqueue_run(
            other,
            EnqueueRun(
                connection_id=other_stack_connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="enqueue-1",
            ),
        )
        finished = self.stack.jobs.execute_run(
            other, ExecuteRun(run_id=queued.run_id, idempotency_key="execute-1")
        )

        self.assertEqual(finished.pages_fetched, 1)
        self.assertEqual(finished.quota_spent, 3)


class RetryTests(PolicyFixture):
    def test_a_transient_failure_requeues_with_the_exact_backoff(self) -> None:
        self.stack.data_gateway.failure = ProviderUnavailable()

        requeued = self.execute()

        self.assertEqual(requeued.status, RunStatus.QUEUED)
        self.assertEqual(requeued.attempt, 2)
        self.assertEqual(requeued.next_attempt_at, NOW + BACKOFF_SCHEDULE[0])
        self.assertIsNone(requeued.finished_at)

    def test_a_requeued_run_cannot_start_before_its_backoff(self) -> None:
        self.stack.data_gateway.failure = ProviderUnavailable()
        self.execute()

        with self.assertRaises(CollectionJobsError) as raised:
            self.execute(key="execute-2")

        self.assertEqual(raised.exception.code, "INVALID_RUN_TRANSITION")
        self.assertTrue(raised.exception.retryable)

    def test_three_attempts_then_a_safe_failure(self) -> None:
        self.stack.data_gateway.failure = ProviderUnavailable()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.stack.clock.advance(timedelta(hours=1))
            run = self.execute(key=f"execute-{attempt}")

        self.assertEqual(run.attempt, MAX_ATTEMPTS)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.failure_reason, RunFailureReason.PROVIDER_UNAVAILABLE)
        self.assertIsNotNone(run.finished_at)

    def test_a_successful_retry_publishes_normally(self) -> None:
        self.stack.data_gateway.failure = ProviderUnavailable()
        self.execute()
        self.stack.data_gateway.failure = None
        self.stack.clock.advance(BACKOFF_SCHEDULE[0])

        finished = self.execute(key="execute-2")

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        freshness = self.stack.channel_data.get_freshness(self.owner, "UC_channel_1")
        self.assertIsNotNone(freshness.subscribers.latest_accepted_success)


class FailClosedTests(PolicyFixture):
    def test_a_revoked_grant_fails_the_run_without_retrying(self) -> None:
        self.stack.connections.report_credential_invalidation(
            self.owner,
            ReportCredentialInvalidation(
                connection_id=self.connection.connection_id, idempotency_key="i1"
            ),
        )

        finished = self.execute()

        self.assertEqual(finished.status, RunStatus.FAILED)
        self.assertEqual(finished.failure_reason, RunFailureReason.REAUTH_REQUIRED)
        self.assertEqual(finished.attempt, 1)
        self.assertEqual(self.collection_states(), [])

    def test_quota_exhaustion_is_never_auto_retried(self) -> None:
        stack = build_stack(daily_quota_units=5)
        owner = context()
        connection = stack.connect(owner)
        queued = stack.jobs.enqueue_run(
            owner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="e1",
            ),
        )

        finished = stack.jobs.execute_run(
            owner, ExecuteRun(run_id=queued.run_id, idempotency_key="x1")
        )

        self.assertEqual(finished.status, RunStatus.PARTIAL)
        self.assertIsNone(finished.next_attempt_at)


class CancellationTests(PolicyFixture):
    def test_a_queued_run_can_be_cancelled(self) -> None:
        cancelled = self.stack.jobs.cancel_run(
            self.owner, CancelRun(run_id=self.run.run_id, idempotency_key="c1")
        )

        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertEqual(cancelled.failure_reason, RunFailureReason.CANCELLED)
        self.assertEqual(cancelled.finished_at, NOW)

    def test_a_cancelled_run_cannot_be_executed(self) -> None:
        self.stack.jobs.cancel_run(
            self.owner, CancelRun(run_id=self.run.run_id, idempotency_key="c1")
        )

        with self.assertRaises(CollectionJobsError) as raised:
            self.execute()

        self.assertEqual(raised.exception.code, "INVALID_RUN_TRANSITION")

    def test_cancelling_a_terminal_run_fails_without_mutation(self) -> None:
        finished = self.execute()

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.cancel_run(
                self.owner, CancelRun(run_id=self.run.run_id, idempotency_key="c1")
            )

        self.assertEqual(raised.exception.code, "INVALID_RUN_TRANSITION")
        self.assertEqual(
            self.stack.jobs.get_run(self.owner, self.run.run_id).status, finished.status
        )

    def test_cancellation_replay_is_idempotent(self) -> None:
        first = self.stack.jobs.cancel_run(
            self.owner, CancelRun(run_id=self.run.run_id, idempotency_key="c1")
        )
        second = self.stack.jobs.cancel_run(
            self.owner, CancelRun(run_id=self.run.run_id, idempotency_key="c1")
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
