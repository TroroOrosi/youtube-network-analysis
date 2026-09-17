from __future__ import annotations

import json
import unittest
from datetime import timedelta

from collection_jobs.models import (
    CancelRun,
    CreateSchedule,
    DEFAULT_DAILY_QUOTA_UNITS,
    EnqueueRun,
    ExecuteRun,
    RunKind,
    RunStatus,
)
from collection_jobs.service import CollectionJobsService
from collection_jobs.tests.support import build_stack, context


class FakeStore:
    """One document, exactly what a restart would find on disk."""

    def __init__(self) -> None:
        self.document: str | None = None
        self.saves = 0
        self.failure: Exception | None = None

    def load(self) -> str | None:
        return self.document

    def save(self, document: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.document = document
        self.saves += 1


class RestartFixture(unittest.TestCase):
    quota_units: int | None = None

    def setUp(self) -> None:
        self.store = FakeStore()
        self.stack = build_stack(daily_quota_units=self.quota_units)
        self.owner = context()
        self.connection = self.stack.connect(self.owner)
        self.jobs = self.restart()

    def restart(self) -> CollectionJobsService:
        self.jobs = CollectionJobsService(
            clock=self.stack.clock,
            tokens=self.stack.jobs._tokens,
            broker=self.stack.connections,
            targets=self.stack.connections,
            channel_data=self.stack.channel_data,
            daily_quota_units=self.quota_units or DEFAULT_DAILY_QUOTA_UNITS,
            page_size=2,
            state_store=self.store,
        )
        return self.jobs

    def enqueue(self, key: str = "enqueue-1"):
        return self.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key=key,
            ),
        )


class RunRestartTests(RestartFixture):
    def test_a_finished_run_outlives_the_process(self) -> None:
        run = self.enqueue()
        self.jobs.execute_run(
            self.owner, ExecuteRun(run_id=run.run_id, idempotency_key="execute-1")
        )

        self.restart()

        restored = self.jobs.get_run(self.owner, run.run_id)
        self.assertEqual(restored.status, RunStatus.SUCCEEDED)

    def test_a_queued_run_is_still_queued_after_a_restart(self) -> None:
        run = self.enqueue()

        self.restart()

        self.assertEqual(self.jobs.get_run(self.owner, run.run_id).status, RunStatus.QUEUED)

    def test_a_replayed_enqueue_is_still_answered_once(self) -> None:
        run = self.enqueue()

        self.restart()

        self.assertEqual(self.enqueue().run_id, run.run_id)
        self.assertEqual(len(self.jobs.list_runs(self.owner).items), 1)

    def test_a_schedule_outlives_the_process(self) -> None:
        created = self.jobs.create_schedule(
            self.owner,
            CreateSchedule(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                interval=timedelta(days=1),
                idempotency_key="schedule-1",
            ),
        )

        self.restart()

        schedules = self.jobs.list_schedules(self.owner)
        self.assertEqual(
            [schedule.schedule_id for schedule in schedules.items],
            [created.schedule_id],
        )


class SuspendedRunTests(RestartFixture):
    """A run that stopped for want of units, restarted in another process.

    This is the case the whole resume mechanism exists for: the collection is
    larger than a day of quota, and the process that started it is long gone by
    the time the budget refills. What it covered has to still count, and what it
    covered must not be fetched again.
    """

    quota_units = 12

    def owner_content(self, key: str = "content-1"):
        return self.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.OWNER_CONTENT,
                idempotency_key=key,
            ),
        )

    def test_a_run_that_ran_out_of_units_continues_in_the_next_process(self) -> None:
        run = self.owner_content()
        stopped = self.jobs.execute_run(
            self.owner, ExecuteRun(run_id=run.run_id, idempotency_key="execute-1")
        )
        self.assertEqual(stopped.status, RunStatus.QUEUED)
        self.assertIsNotNone(stopped.next_attempt_at)

        self.restart()
        self.stack.clock.advance(timedelta(days=1))
        finished = self.jobs.execute_run(
            self.owner, ExecuteRun(run_id=run.run_id, idempotency_key="execute-2")
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        covered = [
            call.video_id
            for call in self.stack.data_gateway.calls
            if call.operation == "LIST_VIDEO_COMMENT_AUTHORS"
        ]
        self.assertEqual(sorted(set(covered)), ["video-1", "video-2", "video-3"])
        self.assertEqual(len(covered), len(set(covered)))


class SpentQuotaTests(RestartFixture):
    """One call costs more than what a run leaves behind, so the ledger shows."""

    quota_units = 5

    def test_the_quota_already_spent_is_still_counted(self) -> None:
        first = self.enqueue()
        spent = self.jobs.execute_run(
            self.owner, ExecuteRun(run_id=first.run_id, idempotency_key="execute-1")
        )

        # Explicit cancellation allows a new run; it must not refund quota.
        self.jobs.cancel_run(self.owner, CancelRun(run_id=first.run_id, idempotency_key="cancel-1"))
        self.restart()

        second = self.enqueue("enqueue-2")
        finished = self.jobs.execute_run(
            self.owner, ExecuteRun(run_id=second.run_id, idempotency_key="execute-2")
        )
        self.assertGreater(spent.quota_spent, 0)
        self.assertEqual(finished.quota_spent, 0)


class DocumentTests(RestartFixture):
    def test_an_empty_store_is_not_written_until_something_happens(self) -> None:
        self.assertIsNone(self.store.document)
        self.assertEqual(self.store.saves, 0)

    def test_a_document_from_a_newer_version_is_refused(self) -> None:
        self.store.document = json.dumps({"version": 99})

        with self.assertRaises(ValueError):
            self.restart()

    def test_an_unreadable_document_is_refused(self) -> None:
        self.store.document = "{not json"

        with self.assertRaises(ValueError):
            self.restart()

    def test_a_service_without_a_store_still_works(self) -> None:
        run = self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="enqueue-plain",
            ),
        )

        self.assertEqual(run.status, RunStatus.QUEUED)


class FlushFailureTests(RestartFixture):
    """A write that never landed must not be believed by this process either.

    The store can refuse: the document outgrew its limit, or the network went
    away mid-save. What must not survive that is the change itself. If the
    process kept a run the store has never heard of, every later write would be
    built on top of it, and a restart would produce a different history than
    the one the caller was shown.
    """

    def schedule(self, key: str = "schedule-1"):
        return self.jobs.create_schedule(
            self.owner,
            CreateSchedule(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                interval=timedelta(days=1),
                idempotency_key=key,
            ),
        )

    def test_a_refused_write_leaves_the_state_the_store_still_has(self) -> None:
        first = self.schedule("schedule-1")
        self.store.failure = RuntimeError("document too large")

        with self.assertRaises(RuntimeError):
            self.schedule("schedule-2")

        self.store.failure = None
        listed = self.jobs.list_schedules(self.owner)
        self.assertEqual(
            [item.schedule_id for item in listed.items], [first.schedule_id]
        )

    def test_the_rolled_back_state_is_what_a_restart_reads(self) -> None:
        self.schedule("schedule-1")
        self.store.failure = RuntimeError("document too large")
        with self.assertRaises(RuntimeError):
            self.schedule("schedule-2")
        self.store.failure = None

        before = [
            item.schedule_id for item in self.jobs.list_schedules(self.owner).items
        ]
        self.restart()

        self.assertEqual(
            [item.schedule_id for item in self.jobs.list_schedules(self.owner).items],
            before,
        )

    def test_a_first_write_that_never_landed_leaves_nothing_behind(self) -> None:
        """Nothing was saved yet, so the rollback has no document to go back to."""

        self.store.failure = RuntimeError("network gone")

        with self.assertRaises(RuntimeError):
            self.schedule("schedule-1")

        self.store.failure = None
        self.assertIsNone(self.store.document)
        self.assertEqual(self.jobs.list_schedules(self.owner).items, ())


if __name__ == "__main__":
    unittest.main()
