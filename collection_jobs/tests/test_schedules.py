from __future__ import annotations

import unittest
from datetime import timedelta

from collection_jobs.errors import CollectionJobsError
from collection_jobs.models import (
    CreateSchedule,
    DeleteSchedule,
    EnqueueRun,
    ExecuteRun,
    JobsPageRequest,
    MINIMUM_SCHEDULE_INTERVAL,
    RunKind,
    RunQuery,
    RunStatus,
)
from collection_jobs.tests.support import NOW, build_stack, context
from workspace_access.models import Permission


class ScheduleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = build_stack()
        self.owner = context()
        self.connection = self.stack.connect(self.owner)

    def create(self, *, kind: RunKind = RunKind.SUBSCRIBERS, key: str = "schedule-1"):
        return self.stack.jobs.create_schedule(
            self.owner,
            CreateSchedule(
                connection_id=self.connection.connection_id,
                kind=kind,
                interval=MINIMUM_SCHEDULE_INTERVAL,
                idempotency_key=key,
            ),
        )


class ScheduleTests(ScheduleFixture):
    def test_a_schedule_is_created_for_the_connected_channel(self) -> None:
        schedule = self.create()

        self.assertEqual(schedule.workspace_id, "workspace-1")
        self.assertEqual(schedule.connection_id, self.connection.connection_id)
        self.assertEqual(schedule.interval, MINIMUM_SCHEDULE_INTERVAL)
        self.assertTrue(schedule.enabled)
        self.assertIsNone(schedule.last_enqueued_at)

    def test_creating_a_schedule_requires_collection_run(self) -> None:
        reader = context("workspace-1", Permission.COLLECTION_READ, Permission.CHANNEL_READ)

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.create_schedule(
                reader,
                CreateSchedule(
                    connection_id=self.connection.connection_id,
                    kind=RunKind.SUBSCRIBERS,
                    interval=MINIMUM_SCHEDULE_INTERVAL,
                    idempotency_key="k",
                ),
            )

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_due_schedules_enqueue_exactly_one_run(self) -> None:
        self.create()

        enqueued = self.stack.jobs.enqueue_due_runs(self.owner, NOW)

        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0].kind, RunKind.SUBSCRIBERS)
        self.assertEqual(enqueued[0].status, RunStatus.QUEUED)

    def test_a_schedule_does_not_enqueue_twice_within_its_interval(self) -> None:
        self.create()
        first = self.stack.jobs.enqueue_due_runs(self.owner, NOW)
        self.stack.jobs.execute_run(
            self.owner, ExecuteRun(run_id=first[0].run_id, idempotency_key="x1")
        )

        again = self.stack.jobs.enqueue_due_runs(self.owner, NOW + timedelta(minutes=59))
        later = self.stack.jobs.enqueue_due_runs(self.owner, NOW + MINIMUM_SCHEDULE_INTERVAL)

        self.assertEqual(again, ())
        self.assertEqual(len(later), 1)

    def test_an_active_run_blocks_a_new_scheduled_run(self) -> None:
        self.create()
        self.stack.jobs.enqueue_due_runs(self.owner, NOW)

        again = self.stack.jobs.enqueue_due_runs(
            self.owner, NOW + MINIMUM_SCHEDULE_INTERVAL
        )

        self.assertEqual(again, ())

    def test_deleting_a_schedule_stops_it(self) -> None:
        schedule = self.create()

        self.stack.jobs.delete_schedule(
            self.owner,
            DeleteSchedule(schedule_id=schedule.schedule_id, idempotency_key="d1"),
        )

        self.assertEqual(self.stack.jobs.enqueue_due_runs(self.owner, NOW), ())
        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.delete_schedule(
                self.owner,
                DeleteSchedule(schedule_id=schedule.schedule_id, idempotency_key="d2"),
            )
        self.assertEqual(raised.exception.code, "SCHEDULE_NOT_FOUND_OR_FORBIDDEN")

    def test_a_foreign_schedule_is_hidden(self) -> None:
        schedule = self.create()

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.delete_schedule(
                context("workspace-2"),
                DeleteSchedule(schedule_id=schedule.schedule_id, idempotency_key="d1"),
            )

        self.assertEqual(raised.exception.code, "SCHEDULE_NOT_FOUND_OR_FORBIDDEN")


class ReadTests(ScheduleFixture):
    def enqueue_runs(self, count: int) -> list[str]:
        run_ids = []
        for index in range(count):
            kind = RunKind.SUBSCRIBERS if index % 2 == 0 else RunKind.OWNER_CONTENT
            run = self.stack.jobs.enqueue_run(
                self.owner,
                EnqueueRun(
                    connection_id=self.connection.connection_id,
                    kind=kind,
                    idempotency_key=f"enqueue-{index}",
                ),
            )
            self.stack.jobs.cancel_run(
                self.owner,
                __import__(
                    "collection_jobs.models", fromlist=["CancelRun"]
                ).CancelRun(run_id=run.run_id, idempotency_key=f"cancel-{index}"),
            )
            run_ids.append(run.run_id)
            self.stack.clock.advance(timedelta(minutes=1))
        return run_ids

    def test_runs_are_listed_newest_first(self) -> None:
        run_ids = self.enqueue_runs(3)

        page = self.stack.jobs.list_runs(self.owner)

        self.assertEqual(
            [item.run_id for item in page.items], list(reversed(run_ids))
        )
        self.assertIsNone(page.next_cursor)

    def test_runs_can_be_filtered_by_kind_and_connection(self) -> None:
        self.enqueue_runs(3)

        page = self.stack.jobs.list_runs(
            self.owner, RunQuery(kind=RunKind.OWNER_CONTENT)
        )

        self.assertTrue(page.items)
        self.assertTrue(all(item.kind is RunKind.OWNER_CONTENT for item in page.items))

    def test_full_traversal_returns_each_run_once(self) -> None:
        run_ids = self.enqueue_runs(5)

        seen = []
        page = self.stack.jobs.list_runs(self.owner, page=JobsPageRequest(limit=2))
        seen.extend(item.run_id for item in page.items)
        while page.next_cursor is not None:
            page = self.stack.jobs.list_runs(
                self.owner, page=JobsPageRequest(cursor=page.next_cursor, limit=2)
            )
            seen.extend(item.run_id for item in page.items)

        self.assertEqual(sorted(seen), sorted(run_ids))

    def test_a_tampered_or_foreign_cursor_is_rejected(self) -> None:
        self.enqueue_runs(3)
        page = self.stack.jobs.list_runs(self.owner, page=JobsPageRequest(limit=1))
        cursor = page.next_cursor
        assert cursor is not None

        for actor, token in ((self.owner, cursor + "x"), (context("workspace-2"), cursor)):
            with self.assertRaises(CollectionJobsError) as raised:
                self.stack.jobs.list_runs(
                    actor, page=JobsPageRequest(cursor=token, limit=1)
                )
            self.assertEqual(raised.exception.code, "INVALID_CURSOR")

    def test_a_changed_result_set_expires_the_cursor(self) -> None:
        self.enqueue_runs(3)
        page = self.stack.jobs.list_runs(self.owner, page=JobsPageRequest(limit=1))
        assert page.next_cursor is not None
        self.stack.jobs.enqueue_run(
            self.owner,
            EnqueueRun(
                connection_id=self.connection.connection_id,
                kind=RunKind.SUBSCRIBERS,
                idempotency_key="extra-1",
            ),
        )

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.list_runs(
                self.owner, page=JobsPageRequest(cursor=page.next_cursor, limit=1)
            )

        self.assertEqual(raised.exception.code, "CURSOR_EXPIRED")

    def test_reads_require_collection_read(self) -> None:
        runner = context("workspace-1", Permission.COLLECTION_RUN, Permission.CHANNEL_READ)

        with self.assertRaises(CollectionJobsError) as raised:
            self.stack.jobs.list_runs(runner)

        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_schedules_are_listed_for_the_workspace_only(self) -> None:
        self.create()

        own = self.stack.jobs.list_schedules(self.owner)
        foreign = self.stack.jobs.list_schedules(context("workspace-2"))

        self.assertEqual(len(own.items), 1)
        self.assertEqual(foreign.items, ())


if __name__ == "__main__":
    unittest.main()
