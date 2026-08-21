# Implementation Plan: collection-jobs

Status: approved
Date: 2026-08-21
Specification: [`SPEC-collection-jobs.md`](../SPEC-collection-jobs.md)
Capability: [`collection-jobs`](../CAPABILITY_MAP.md)

## Overview

Implement scheduling, queued runs, quota-aware traversal, deterministic
retries, cancellation, and run status as a standard-library reference over the
existing `channel-connections` execution broker and `channel-data` collection
writer. Execution is synchronous and caller-driven: there is no thread, timer,
queue, or worker platform in this slice.

## Architecture decisions

- `collection_jobs.models` owns immutable run, schedule, quota, page, and
  command values. It imports no provider, HTTP, database, or analytics type.
- `collection_jobs.ports` exposes `CollectionRunner`, `CollectionScheduler`,
  `CollectionRunReader`, and `CollectionJobsAdministrator`, plus the two
  collaborator protocols it consumes and a deterministic clock and token source.
- `CollectionJobsService` is the public in-memory facade with one reentrant
  lock around every transition, exactly like the previous modules.
- A run holds no authority. `execute_run` asks the broker for a fresh
  execution authority, uses it only inside that call, and never stores it.
- `OWNER_CONTENT` publishes the video inventory and finishes it before covering
  comments, because `channel-data` binds coverage to the accepted inventory.
- Quota is a per-workspace, per-UTC-day ledger. A run checks affordability
  before each brokered call rather than after spending.
- A partial run promotes nothing and does not resume a provider cursor, because
  an accepted generation requires a complete traversal.
- Retention, cascade, cursors, and idempotency reuse the shapes proven in
  `channel-data` and `channel-connections`.

## Sequence

1. Contracts, errors, ports.
2. Run enqueue and subscriber execution.
3. Quota, retries, cancellation, fail-closed reauthorization.
4. Owner content execution across both phases.
5. Schedules, reads, bounded pagination.
6. Tenant isolation, retention, cascade, integration, README, review.

## Verification commands

```powershell
python -m unittest discover -s collection_jobs/tests -v
python -m unittest discover -s channel_connections/tests -v
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q collection_jobs channel_connections channel_data workspace_access subscriber_analytics
git diff --check
```

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A job obtains or stores a credential | Critical | Only the broker touches credentials; the run stores no authority and the tests assert it |
| A partial traversal is promoted | High | Finish `channel-data` `PARTIAL` on any incomplete outcome and assert prior data stays readable |
| Quota overrun against the real provider | High | Check affordability before every call, deduct the reported cost, and stop cleanly at the exact boundary |
| A failure leaves a run stuck `RUNNING` | High | Every execution path ends in a terminal or requeued state under one lock |
| Retry storms | Medium | Deterministic 1/5/25 minute backoff, three attempts, and no auto-retry for quota or reauthorization |
| Cross-tenant leakage through quota or schedules | High | Workspace-prefixed keys everywhere plus identical-identifier isolation fixtures |
| In-memory semantics mistaken for a queue | Medium | Document that execution is caller-driven and keep worker/queue selection an explicit later gate |

## Scope discipline

Untouched: real provider calls, HTTP clients, SDK choice, queue or worker
platform, database, scheduler daemon, Web routes, UI, analytics behavior, and
`channel-data` retention rules.
