from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from inspect import signature

from collection_jobs.errors import CollectionJobsError, ErrorCode
from collection_jobs.models import (
    BACKOFF_SCHEDULE,
    CancelRun,
    CollectionRun,
    CollectionSchedule,
    CreateSchedule,
    DEFAULT_DAILY_QUOTA_UNITS,
    DeleteSchedule,
    DeleteWorkspaceJobs,
    EnqueueRun,
    ExecuteRun,
    JobsPageRequest,
    JobsRetentionReport,
    MAX_ATTEMPTS,
    MINIMUM_SCHEDULE_INTERVAL,
    QuotaBudget,
    RunFailureReason,
    RunKind,
    RunStatus,
    RunQuery,
)
from collection_jobs.ports import (
    CollectionJobsAdministrator,
    CollectionRunReader,
    CollectionRunner,
    CollectionScheduler,
)
from workspace_access.models import WorkspaceContext


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def run(**overrides: object) -> CollectionRun:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "workspace_id": "workspace-1",
        "connection_id": "connection-1",
        "provider_channel_id": "UC_channel_1",
        "kind": RunKind.SUBSCRIBERS,
        "status": RunStatus.QUEUED,
        "attempt": 1,
        "enqueued_at": NOW,
        "started_at": None,
        "finished_at": None,
        "pages_fetched": 0,
        "quota_spent": 0,
        "failure_reason": None,
        "next_attempt_at": None,
    }
    fields.update(overrides)
    return CollectionRun(**fields)  # type: ignore[arg-type]


class ErrorContractTests(unittest.TestCase):
    def test_error_codes_are_stable_machine_values(self) -> None:
        self.assertEqual(
            [code.value for code in ErrorCode],
            [
                "INVALID_INPUT",
                "PERMISSION_DENIED",
                "RUN_NOT_FOUND_OR_FORBIDDEN",
                "SCHEDULE_NOT_FOUND_OR_FORBIDDEN",
                "RUN_ALREADY_ACTIVE",
                "INVALID_RUN_TRANSITION",
                "IDEMPOTENCY_CONFLICT",
                "INVALID_CURSOR",
                "CURSOR_EXPIRED",
            ],
        )

    def test_errors_expose_only_stable_safe_fields(self) -> None:
        error = CollectionJobsError(
            ErrorCode.RUN_ALREADY_ACTIVE,
            message="A run is already active",
            field="command.kind",
            retryable=True,
            reason_code="QUOTA_EXHAUSTED",
        )

        self.assertEqual(error.code, "RUN_ALREADY_ACTIVE")
        self.assertTrue(error.retryable)
        self.assertEqual(error.reason_code, "QUOTA_EXHAUSTED")
        self.assertTrue(error.correlation_id.startswith("error_"))


class PolicyConstantTests(unittest.TestCase):
    def test_policy_constants_match_the_approved_specification(self) -> None:
        self.assertEqual(DEFAULT_DAILY_QUOTA_UNITS, 10000)
        self.assertEqual(MINIMUM_SCHEDULE_INTERVAL, timedelta(hours=1))
        self.assertEqual(
            BACKOFF_SCHEDULE,
            (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=25)),
        )
        self.assertEqual(MAX_ATTEMPTS, 3)


class EnumContractTests(unittest.TestCase):
    def test_run_enums_are_stable(self) -> None:
        self.assertEqual(
            [item.value for item in RunKind],
            ["SUBSCRIBERS", "OWNER_CONTENT", "AUDIENCE_NETWORK"],
        )
        self.assertEqual(
            [item.value for item in RunStatus],
            ["QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"],
        )
        self.assertEqual(
            [item.value for item in RunFailureReason],
            [
                "QUOTA_EXHAUSTED",
                "PROVIDER_UNAVAILABLE",
                "REAUTH_REQUIRED",
                "CANCELLED",
                "UNEXPECTED_FAILURE",
            ],
        )


class RunContractTests(unittest.TestCase):
    def test_run_is_immutable_and_slotted(self) -> None:
        value = run()

        with self.assertRaises(FrozenInstanceError):
            value.status = RunStatus.RUNNING  # type: ignore[misc]
        self.assertFalse(hasattr(value, "__dict__"))

    def test_run_normalizes_datetimes_to_utc(self) -> None:
        local = datetime(2026, 8, 21, 21, 0, tzinfo=timezone(timedelta(hours=9)))

        value = run(enqueued_at=local)

        self.assertEqual(value.enqueued_at, NOW)
        self.assertEqual(value.enqueued_at.tzinfo, UTC)

    def test_run_rejects_naive_times_and_unbounded_identifiers(self) -> None:
        for field, value in (
            ("enqueued_at", datetime(2026, 8, 21, 12, 0)),
            ("run_id", ""),
            ("connection_id", "c" * 300),
            ("provider_channel_id", " UC_channel_1"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(CollectionJobsError) as raised:
                    run(**{field: value})
                self.assertEqual(raised.exception.field, field)

    def test_a_terminal_run_requires_a_finished_time(self) -> None:
        with self.assertRaises(CollectionJobsError) as raised:
            run(status=RunStatus.SUCCEEDED)

        self.assertEqual(raised.exception.field, "finished_at")

    def test_only_a_failed_or_partial_run_carries_a_reason(self) -> None:
        run(
            status=RunStatus.PARTIAL,
            started_at=NOW,
            finished_at=NOW,
            failure_reason=RunFailureReason.QUOTA_EXHAUSTED,
        )

        with self.assertRaises(CollectionJobsError) as raised:
            run(
                status=RunStatus.SUCCEEDED,
                started_at=NOW,
                finished_at=NOW,
                failure_reason=RunFailureReason.QUOTA_EXHAUSTED,
            )
        self.assertEqual(raised.exception.field, "failure_reason")

    def test_counters_must_be_non_negative_integers(self) -> None:
        for field in ("pages_fetched", "quota_spent"):
            with self.subTest(field=field):
                with self.assertRaises(CollectionJobsError) as raised:
                    run(**{field: -1})
                self.assertEqual(raised.exception.field, field)

    def test_attempt_starts_at_one(self) -> None:
        with self.assertRaises(CollectionJobsError) as raised:
            run(attempt=0)

        self.assertEqual(raised.exception.field, "attempt")


class ScheduleAndQuotaContractTests(unittest.TestCase):
    def test_schedule_enforces_the_minimum_interval(self) -> None:
        CollectionSchedule(
            schedule_id="schedule-1",
            workspace_id="workspace-1",
            connection_id="connection-1",
            kind=RunKind.SUBSCRIBERS,
            interval=MINIMUM_SCHEDULE_INTERVAL,
            enabled=True,
            created_at=NOW,
            last_enqueued_at=None,
        )

        with self.assertRaises(CollectionJobsError) as raised:
            CollectionSchedule(
                schedule_id="schedule-1",
                workspace_id="workspace-1",
                connection_id="connection-1",
                kind=RunKind.SUBSCRIBERS,
                interval=timedelta(minutes=59),
                enabled=True,
                created_at=NOW,
                last_enqueued_at=None,
            )
        self.assertEqual(raised.exception.field, "interval")

    def test_quota_budget_requires_positive_units(self) -> None:
        budget = QuotaBudget(workspace_id="workspace-1", daily_units=1)

        self.assertEqual(budget.daily_units, 1)
        for units in (0, -5, True):
            with self.subTest(units=units):
                with self.assertRaises(CollectionJobsError) as raised:
                    QuotaBudget(workspace_id="workspace-1", daily_units=units)
                self.assertEqual(raised.exception.field, "daily_units")

    def test_retention_report_counts_are_non_negative(self) -> None:
        report = JobsRetentionReport(
            runs_removed=1, quota_entries_removed=0, idempotency_records_removed=2
        )

        self.assertEqual(report.runs_removed, 1)
        with self.assertRaises(CollectionJobsError):
            JobsRetentionReport(
                runs_removed=-1, quota_entries_removed=0, idempotency_records_removed=0
            )


class CommandAndPageContractTests(unittest.TestCase):
    def test_commands_require_bounded_idempotency_keys(self) -> None:
        builders = (
            lambda key: EnqueueRun(
                connection_id="connection-1", kind=RunKind.SUBSCRIBERS, idempotency_key=key
            ),
            lambda key: ExecuteRun(run_id="run-1", idempotency_key=key),
            lambda key: CancelRun(run_id="run-1", idempotency_key=key),
            lambda key: CreateSchedule(
                connection_id="connection-1",
                kind=RunKind.SUBSCRIBERS,
                interval=MINIMUM_SCHEDULE_INTERVAL,
                idempotency_key=key,
            ),
            lambda key: DeleteSchedule(schedule_id="schedule-1", idempotency_key=key),
            lambda key: DeleteWorkspaceJobs(idempotency_key=key),
        )
        for build in builders:
            with self.subTest(command=build):
                build("key-1")
                with self.assertRaises(CollectionJobsError) as raised:
                    build("")
                self.assertEqual(raised.exception.field, "idempotency_key")

    def test_page_request_defaults_and_bounds(self) -> None:
        self.assertEqual(JobsPageRequest().limit, 50)
        for limit in (0, 101, True):
            with self.subTest(limit=limit):
                with self.assertRaises(CollectionJobsError) as raised:
                    JobsPageRequest(limit=limit)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.field, "limit")

    def test_run_query_filters_are_validated(self) -> None:
        RunQuery()
        RunQuery(connection_id="connection-1", kind=RunKind.SUBSCRIBERS)

        with self.assertRaises(CollectionJobsError) as raised:
            RunQuery(connection_id="")
        self.assertEqual(raised.exception.field, "connection_id")


class PortContractTests(unittest.TestCase):
    def test_every_tenant_operation_takes_a_workspace_context(self) -> None:
        protocols = (
            (CollectionRunner, ("enqueue_run", "execute_run", "cancel_run")),
            (
                CollectionScheduler,
                ("create_schedule", "delete_schedule", "enqueue_due_runs"),
            ),
            (CollectionRunReader, ("get_run", "list_runs", "list_schedules")),
            (
                CollectionJobsAdministrator,
                ("delete_workspace_jobs", "purge_retention"),
            ),
        )
        for protocol, methods in protocols:
            for name in methods:
                with self.subTest(protocol=protocol.__name__, method=name):
                    parameters = signature(getattr(protocol, name)).parameters
                    self.assertEqual(list(parameters)[:2], ["self", "context"])
                    self.assertEqual(
                        parameters["context"].annotation, WorkspaceContext.__name__
                    )


if __name__ == "__main__":
    unittest.main()
