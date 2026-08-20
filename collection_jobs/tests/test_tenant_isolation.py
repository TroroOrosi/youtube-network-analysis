from __future__ import annotations

import unittest
from datetime import timedelta

from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import (
    CancelRun,
    CreateSchedule,
    DeleteSchedule,
    DeleteWorkspaceJobs,
    EnqueueRun,
    ExecuteRun,
    JobsRetentionReport,
    MINIMUM_SCHEDULE_INTERVAL,
    RunKind,
    RunStatus,
)
from collection_jobs.tests.support import NOW, build_stack, context


class TwoWorkspaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.first = context("workspace-1")
        self.second = context("workspace-2")
        self.first_connection = self.stack.connect(self.first, key="connect-1")
        self.second_connection = self.stack.connect(self.second, key="connect-1")

    def enqueue(self, actor, connection, *, key="enqueue-1", kind=RunKind.SUBSCRIBERS):
        return self.stack.jobs.enqueue_run(
            actor,
            EnqueueRun(
                connection_id=connection.connection_id, kind=kind, idempotency_key=key
            ),
        )


class TenantIsolationTests(TwoWorkspaceFixture):
    def test_identical_keys_produce_independent_runs(self) -> None:
        first = self.enqueue(self.first, self.first_connection)
        second = self.enqueue(self.second, self.second_connection)

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.workspace_id, "workspace-1")
        self.assertEqual(second.workspace_id, "workspace-2")
        self.assertEqual(
            first.provider_channel_id, second.provider_channel_id
        )

    def test_a_foreign_run_is_hidden_from_every_operation(self) -> None:
        run = self.enqueue(self.first, self.first_connection)

        operations = (
            lambda: self.stack.jobs.get_run(self.second, run.run_id),
            lambda: self.stack.jobs.execute_run(
                self.second, ExecuteRun(run_id=run.run_id, idempotency_key="x")
            ),
            lambda: self.stack.jobs.cancel_run(
                self.second, CancelRun(run_id=run.run_id, idempotency_key="x")
            ),
        )
        messages = set()
        for operation in operations:
            with self.assertRaises(CollectionJobsError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "RUN_NOT_FOUND_OR_FORBIDDEN")
            messages.add(raised.exception.message)

        self.assertEqual(len(messages), 1)
        self.assertEqual(
            self.stack.jobs.get_run(self.first, run.run_id).status, RunStatus.QUEUED
        )

    def test_quota_is_not_shared_between_workspaces(self) -> None:
        limited = build_stack(daily_quota_units=5)
        first = context("workspace-1")
        second = context("workspace-2")
        first_connection = limited.connect(first, key="c1")
        second_connection = limited.connect(second, key="c1")

        first_run = limited.jobs.enqueue_run(
            first,
            EnqueueRun(
                connection_id=first_connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="e1",
            ),
        )
        limited.jobs.execute_run(
            first, ExecuteRun(run_id=first_run.run_id, idempotency_key="x1")
        )
        second_run = limited.jobs.enqueue_run(
            second,
            EnqueueRun(
                connection_id=second_connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="e1",
            ),
        )
        finished = limited.jobs.execute_run(
            second, ExecuteRun(run_id=second_run.run_id, idempotency_key="x1")
        )

        self.assertEqual(finished.pages_fetched, 1)
        self.assertEqual(finished.quota_spent, 3)

    def test_listing_and_schedules_stay_within_a_workspace(self) -> None:
        self.enqueue(self.first, self.first_connection)
        self.stack.jobs.create_schedule(
            self.first,
            CreateSchedule(
                connection_id=self.first_connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                interval=MINIMUM_SCHEDULE_INTERVAL,
                idempotency_key="s1",
            ),
        )

        self.assertEqual(len(self.stack.jobs.list_runs(self.first).items), 1)
        self.assertEqual(self.stack.jobs.list_runs(self.second).items, ())
        self.assertEqual(len(self.stack.jobs.list_schedules(self.first).items), 1)
        self.assertEqual(self.stack.jobs.list_schedules(self.second).items, ())

    def test_a_foreign_schedule_cannot_be_deleted(self) -> None:
        schedule = self.stack.jobs.create_schedule(
            self.first,
            CreateSchedule(
                connection_id=self.first_connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                interval=MINIMUM_SCHEDULE_INTERVAL,
                idempotency_key="s1",
            ),
        )

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.delete_schedule(
                self.second,
                DeleteSchedule(schedule_id=schedule.schedule_id, idempotency_key="d1"),
            )

        self.assertEqual(raised.exception.code, "SCHEDULE_NOT_FOUND_OR_FORBIDDEN")
        self.assertEqual(len(self.stack.jobs.list_schedules(self.first).items), 1)


class RetentionAndCascadeTests(TwoWorkspaceFixture):
    def terminal_run(self, actor, connection, *, key: str) -> str:
        run = self.enqueue(actor, connection, key=key)
        self.stack.jobs.cancel_run(
            actor, CancelRun(run_id=run.run_id, idempotency_key=f"cancel-{key}")
        )
        return run.run_id

    def test_terminal_runs_and_records_expire_at_ninety_days(self) -> None:
        run_id = self.terminal_run(self.first, self.first_connection, key="e1")

        before = self.stack.jobs.purge_retention(
            self.first, NOW + timedelta(days=90) - timedelta(seconds=1)
        )
        self.assertEqual(before.runs_removed, 0)

        report = self.stack.jobs.purge_retention(self.first, NOW + timedelta(days=90))

        self.assertIsInstance(report, JobsRetentionReport)
        self.assertEqual(report.runs_removed, 1)
        self.assertGreaterEqual(report.idempotency_records_removed, 1)
        with self.assertRaises(CollectionJobsError):
            self.stack.jobs.get_run(self.first, run_id)

    def test_an_unresolved_run_is_never_purged(self) -> None:
        run = self.enqueue(self.first, self.first_connection)

        report = self.stack.jobs.purge_retention(self.first, NOW + timedelta(days=400))

        self.assertEqual(report.runs_removed, 0)
        self.assertEqual(
            self.stack.jobs.get_run(self.first, run.run_id).status, RunStatus.QUEUED
        )

    def test_purging_one_workspace_leaves_the_other_intact(self) -> None:
        self.terminal_run(self.first, self.first_connection, key="e1")
        foreign_run = self.terminal_run(self.second, self.second_connection, key="e1")

        self.stack.jobs.purge_retention(self.first, NOW + timedelta(days=90))

        self.assertEqual(
            self.stack.jobs.get_run(self.second, foreign_run).status,
            RunStatus.CANCELLED,
        )

    def test_workspace_cascade_removes_only_its_own_records(self) -> None:
        own_run = self.enqueue(self.first, self.first_connection)
        foreign_run = self.enqueue(self.second, self.second_connection)
        self.stack.jobs.create_schedule(
            self.first,
            CreateSchedule(
                connection_id=self.first_connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                interval=MINIMUM_SCHEDULE_INTERVAL,
                idempotency_key="s1",
            ),
        )

        self.stack.jobs.delete_workspace_jobs(
            self.first, DeleteWorkspaceJobs(idempotency_key="cascade-1")
        )

        with self.assertRaises(CollectionJobsError):
            self.stack.jobs.get_run(self.first, own_run.run_id)
        self.assertEqual(self.stack.jobs.list_schedules(self.first).items, ())
        self.assertEqual(
            self.stack.jobs.get_run(self.second, foreign_run.run_id).run_id,
            foreign_run.run_id,
        )

    def test_cascade_requires_workspace_delete_and_replays_once(self) -> None:
        reader = context(
            "workspace-1",
            *[
                permission
                for permission in self.first.permissions
                if permission.value != "workspace.delete"
            ],
        )

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.delete_workspace_jobs(
                reader, DeleteWorkspaceJobs(idempotency_key="cascade-1")
            )
        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

        self.stack.jobs.delete_workspace_jobs(
            self.first, DeleteWorkspaceJobs(idempotency_key="cascade-1")
        )
        self.stack.jobs.delete_workspace_jobs(
            self.first, DeleteWorkspaceJobs(idempotency_key="cascade-1")
        )

    def test_cascade_does_not_delete_channel_data(self) -> None:
        run = self.enqueue(self.first, self.first_connection)
        self.stack.jobs.execute_run(
            self.first, ExecuteRun(run_id=run.run_id, idempotency_key="x1")
        )

        self.stack.jobs.delete_workspace_jobs(
            self.first, DeleteWorkspaceJobs(idempotency_key="cascade-1")
        )

        freshness = self.stack.channel_data.get_freshness(self.first, "UC_channel_1")
        self.assertIsNotNone(freshness.subscribers.latest_accepted_success)


if __name__ == "__main__":
    unittest.main()
