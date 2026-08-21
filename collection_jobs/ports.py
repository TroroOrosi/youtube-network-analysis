"""Stable synchronous ports for tenant-scoped collection jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from workspace_access.models import WorkspaceContext

from .models import (
    CancelRun,
    CollectionRun,
    CollectionSchedule,
    CreateSchedule,
    DeleteSchedule,
    DeleteWorkspaceJobs,
    EnqueueRun,
    ExecuteRun,
    JobsPage,
    JobsPageRequest,
    JobsRetentionReport,
    RunQuery,
)


class CollectionRunner(Protocol):
    def enqueue_run(
        self, context: WorkspaceContext, command: EnqueueRun
    ) -> CollectionRun: ...

    def execute_run(
        self, context: WorkspaceContext, command: ExecuteRun
    ) -> CollectionRun: ...

    def cancel_run(
        self, context: WorkspaceContext, command: CancelRun
    ) -> CollectionRun: ...


class CollectionScheduler(Protocol):
    def create_schedule(
        self, context: WorkspaceContext, command: CreateSchedule
    ) -> CollectionSchedule: ...

    def delete_schedule(
        self, context: WorkspaceContext, command: DeleteSchedule
    ) -> None: ...

    def enqueue_due_runs(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> tuple[CollectionRun, ...]: ...


class CollectionRunReader(Protocol):
    def get_run(self, context: WorkspaceContext, run_id: str) -> CollectionRun: ...

    def list_runs(
        self,
        context: WorkspaceContext,
        query: RunQuery = RunQuery(),
        page: JobsPageRequest = JobsPageRequest(),
    ) -> JobsPage: ...

    def list_schedules(
        self,
        context: WorkspaceContext,
        page: JobsPageRequest = JobsPageRequest(),
    ) -> JobsPage: ...


class CollectionJobsAdministrator(Protocol):
    def delete_workspace_jobs(
        self, context: WorkspaceContext, command: DeleteWorkspaceJobs
    ) -> None: ...

    def purge_retention(
        self, context: WorkspaceContext, reference_time: datetime
    ) -> JobsRetentionReport: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenGenerator(Protocol):
    def new_token(self) -> str: ...


class StateStore(Protocol):
    """Where this module's state document rests between two processes."""

    def load(self) -> str | None: ...

    def save(self, document: str) -> None: ...
