"""Working what is due, for a caller that is not a person waiting on it."""

from __future__ import annotations

import unittest
from datetime import timedelta

from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import EnqueueRun, RunKind, RunStatus
from collection_jobs.tests.support import NOW, TickingClock, build_stack, context
from workspace_access.models import Permission


class DrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)

    def enqueue(self, kind: RunKind = RunKind.OWNER_CONTENT, key: str = "c1"):
        return self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=kind,
                idempotency_key=key,
            ),
        )

    def test_it_works_every_queued_run_that_is_due(self) -> None:
        self.enqueue(RunKind.SUBSCRIBERS, "c1")
        self.enqueue(RunKind.OWNER_CONTENT, "c2")

        worked = self.stack.jobs.execute_due_runs(self.owner, NOW, 60)

        self.assertEqual(len(worked), 2)
        self.assertEqual({run.status for run in worked}, {RunStatus.SUCCEEDED})

    def test_it_leaves_a_run_whose_moment_has_not_come(self) -> None:
        """A run waiting for tomorrow's units is not work this call can do."""

        limited = build_stack(daily_quota_units=6)
        owner = context()
        connection = limited.connect(owner)
        limited.jobs.enqueue_run(
            owner,
            EnqueueRun(
                connection_id=connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key="c1",
            ),
        )
        limited.jobs.execute_due_runs(owner, NOW, 60)

        again = limited.jobs.execute_due_runs(owner, NOW, 60)

        self.assertEqual(again, ())
        self.assertEqual(limited.jobs.due_workspace_ids(NOW), ())
        self.assertEqual(
            limited.jobs.due_workspace_ids(NOW + timedelta(days=1)), ("workspace-1",)
        )

    def test_a_spent_slice_stops_the_drain_rather_than_overrunning_it(self) -> None:
        """The caller said how long it has. A second run is not started past that."""

        sliced = build_stack(jobs_clock=TickingClock(NOW, timedelta(seconds=30)))
        owner = context()
        connection = sliced.connect(owner)
        for index, kind in enumerate((RunKind.SUBSCRIBERS, RunKind.OWNER_CONTENT)):
            sliced.jobs.enqueue_run(
                owner,
                EnqueueRun(
                    connection_id=connection.connection_id,
                    kind=kind,
                    idempotency_key=f"c{index}",
                ),
            )

        worked = sliced.jobs.execute_due_runs(owner, NOW, 30)

        self.assertLess(len(worked), 2)

    def test_it_answers_where_work_waits_without_naming_anything_else(self) -> None:
        self.enqueue()
        other = build_stack()

        self.assertEqual(self.stack.jobs.due_workspace_ids(NOW), ("workspace-1",))
        self.assertEqual(other.jobs.due_workspace_ids(NOW), ())

    def test_a_run_still_needs_the_permission_to_be_worked(self) -> None:
        self.enqueue()
        reader = context("workspace-1", Permission.COLLECTION_READ)

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.execute_due_runs(reader, NOW, 60)

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_it_never_reaches_across_a_workspace(self) -> None:
        self.enqueue()
        stranger = context("workspace-2")

        self.assertEqual(self.stack.jobs.execute_due_runs(stranger, NOW, 60), ())


if __name__ == "__main__":
    unittest.main()
