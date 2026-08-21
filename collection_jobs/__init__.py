"""Tenant-scoped scheduling and execution of channel collection runs.

The package root stays small: import values from :mod:`collection_jobs.models`
and the service from :mod:`collection_jobs.service`.
"""

from .errors import CollectionJobsError, ErrorCode
from .ports import (
    CollectionJobsAdministrator,
    CollectionRunReader,
    CollectionRunner,
    CollectionScheduler,
)

__all__ = (
    "CollectionJobsAdministrator",
    "CollectionJobsError",
    "CollectionRunReader",
    "CollectionRunner",
    "CollectionScheduler",
    "ErrorCode",
)
