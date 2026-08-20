from __future__ import annotations

import unittest

from channel_data.models import CollectionKind, CollectionStatus
from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import (
    EnqueueRun,
    ExecuteRun,
    RunKind,
    RunStatus,
)
from collection_jobs.tests.support import NOW, build_stack, context
from workspace_access.models import Permission


class RunFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)

    def enqueue(self, *, kind: RunKind = RunKind.SUBSCRIBERS, key: str = "enqueue-1"):
        return self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id, kind=kind, idempotency_key=key
            ),
        )

    def execute(self, run_id: str, *, key: str = "execute-1"):
        return self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=run_id, idempotency_key=key)
        )


class EnqueueTests(RunFixture):
    def test_enqueue_records_a_queued_run_for_the_connected_channel(self) -> None:
        run = self.enqueue()

        self.assertEqual(run.status, RunStatus.QUEUED)
        self.assertEqual(run.kind, RunKind.SUBSCRIBERS)
        self.assertEqual(run.workspace_id, "workspace-1")
        self.assertEqual(run.connection_id, self.connection.connection_id)
        self.assertEqual(run.provider_channel_id, "UC_channel_1")
        self.assertEqual(run.attempt, 1)
        self.assertEqual(run.enqueued_at, NOW)
        self.assertIsNone(run.finished_at)

    def test_enqueue_requires_collection_run(self) -> None:
        reader = context("workspace-1", Permission.COLLECTION_READ, Permission.CHANNEL_READ)

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.enqueue_run(
                reader,
                EnqueueRun(
                    connection_id=self.connection.connection_id,
                    kind=RunKind.SUBSCRIBERS,
                    idempotency_key="k",
                ),
            )

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_a_missing_or_foreign_connection_cannot_be_enqueued(self) -> None:
        with self.assertRaises(Exception) as missing:
            self.stack.jobs.enqueue_run(
                self.owner,
                EnqueueRun(
                    connection_id="connection_missing",
                    kind=RunKind.SUBSCRIBERS,
                    idempotency_key="k",
                ),
            )

        self.assertEqual(
            getattr(missing.exception, "code", None), "CONNECTION_NOT_FOUND_OR_FORBIDDEN"
        )

    def test_a_second_active_run_of_the_same_kind_is_rejected(self) -> None:
        self.enqueue()

        with self.assertRaises(CollectionJobsError) as raised:
            self.enqueue(key="enqueue-2")

        self.assertEqual(raised.exception.code, "RUN_ALREADY_ACTIVE")

    def test_another_kind_may_be_queued_in_parallel(self) -> None:
        first = self.enqueue()
        second = self.enqueue(kind=RunKind.OWNER_CONTENT, key="enqueue-2")

        self.assertNotEqual(first.run_id, second.run_id)

    def test_exact_replay_returns_the_original_run(self) -> None:
        first = self.enqueue()
        second = self.enqueue()

        self.assertEqual(first, second)

    def test_a_reused_key_with_a_different_payload_conflicts(self) -> None:
        self.enqueue()

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.enqueue_run(
                self.owner,
                EnqueueRun(
                    connection_id=self.connection.connection_id,
                    kind=RunKind.OWNER_CONTENT,
                    idempotency_key="enqueue-1",
                ),
            )

        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")


class SubscriberExecutionTests(RunFixture):
    def test_execution_publishes_one_accepted_snapshot(self) -> None:
        queued = self.enqueue()

        finished = self.execute(queued.run_id)

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        self.assertEqual(finished.run_id, queued.run_id)
        self.assertEqual(finished.started_at, NOW)
        self.assertEqual(finished.finished_at, NOW)
        self.assertIsNone(finished.failure_reason)
        self.assertGreater(finished.pages_fetched, 0)
        self.assertGreater(finished.quota_spent, 0)

        freshness = self.stack.channel_data.get_freshness(self.owner, "UC_channel_1")
        self.assertIsNotNone(freshness.subscribers.latest_accepted_success)

    def test_the_accepted_snapshot_holds_every_provider_row(self) -> None:
        queued = self.enqueue()
        self.execute(queued.run_id)

        page = self.stack.channel_data.list_subscriber_registry(
            self.owner,
            __import__(
                "channel_data.models", fromlist=["SubscriberRegistryQuery"]
            ).SubscriberRegistryQuery(channel_id="UC_channel_1"),
        )

        self.assertEqual(
            {entry.subscriber_channel_id for entry in page.items},
            {row.subscriber_channel_id for row in self.stack.data_gateway.subscribers},
        )

    def test_execution_uses_a_fresh_authority_and_stores_none(self) -> None:
        queued = self.enqueue()

        self.execute(queued.run_id)

        stored = repr(self.stack.jobs._state)
        self.assertNotIn("authority", stored)
        self.assertNotIn("cred_", stored)

    def test_the_channel_data_collection_is_finished_exactly_once(self) -> None:
        queued = self.enqueue()
        self.execute(queued.run_id)

        history = self.stack.channel_data.list_collection_history(
            self.owner,
            __import__(
                "channel_data.models", fromlist=["CollectionHistoryQuery"]
            ).CollectionHistoryQuery(channel_id="UC_channel_1"),
        )
        states = [item for item in history.items if item.kind is CollectionKind.SUBSCRIBERS]

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].status, CollectionStatus.COMPLETE)

    def test_a_queued_run_cannot_be_executed_twice(self) -> None:
        queued = self.enqueue()
        self.execute(queued.run_id)

        with self.assertRaises(CollectionJobsError) as raised:
            self.execute(queued.run_id, key="execute-2")

        self.assertEqual(raised.exception.code, "INVALID_RUN_TRANSITION")

    def test_exact_execution_replay_returns_the_terminal_run(self) -> None:
        queued = self.enqueue()

        first = self.execute(queued.run_id)
        second = self.execute(queued.run_id)

        self.assertEqual(first, second)

    def test_a_missing_or_foreign_run_is_hidden(self) -> None:
        queued = self.enqueue()

        for actor, run_id in (
            (self.owner, "run_missing"),
            (context("workspace-2"), queued.run_id),
        ):
            with self.assertRaises(CollectionJobsError) as raised:
                self.stack.jobs.execute_run(
                    actor, ExecuteRun(run_id=run_id, idempotency_key="x")
                )
            self.assertEqual(raised.exception.code, "RUN_NOT_FOUND_OR_FORBIDDEN")

    def test_execution_requires_collection_run(self) -> None:
        queued = self.enqueue()
        reader = context("workspace-1", Permission.COLLECTION_READ)

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.execute_run(
                reader, ExecuteRun(run_id=queued.run_id, idempotency_key="x")
            )

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")


if __name__ == "__main__":
    unittest.main()
