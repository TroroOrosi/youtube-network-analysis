# Collection Jobs Specification

Status: Approved
Module id: `collection-jobs`
Date: 2026-08-21
Quota-continuation amendment: 2026-09-17 (user-requested multi-day collection)

## Objective

Define how a workspace turns a connected channel into accepted `channel-data`
generations: scheduling, queued runs, quota-aware provider traversal,
deterministic retries, cancellation, and run status — without holding a
credential, contacting a real provider, or choosing a queue, database, or
worker platform.

This module consumes the `channel-connections` execution broker and the
`channel-data` collection writer. It adds no analytics behavior and never
touches raw comment text, author display names, reply relations, or comment
identifiers.

## User-visible behavior

- A member with `collection.run` can enqueue a collection run for a connected
  channel, or create a schedule that enqueues one automatically.
- A run reports its own progress: queued, running, succeeded, partial, failed,
  or cancelled, with a safe reason and the quota it consumed.
- A run that exhausts a daily budget returns to `QUEUED`, preserving its page
  checkpoint and previously accepted data until the next eligible time.
- A run interrupted by a transient provider problem retries on a deterministic
  backoff and only then fails.
- A run against a connection whose grant is no longer valid fails closed and
  reports that reauthorization is required; it never leaves half-published data.
- Everything is tenant scoped: schedules, runs, quota budgets, cursors, and
  deletion never cross a workspace boundary.

## Run model

```python
class RunKind(str, Enum):
    SUBSCRIBERS = "SUBSCRIBERS"
    OWNER_CONTENT = "OWNER_CONTENT"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
```

`SUBSCRIBERS` traverses public subscribers and publishes one snapshot.
`OWNER_CONTENT` publishes the owner video inventory first, then covers every
video in exactly that accepted inventory with per-author comment aggregates.
The two phases live in one run because comment coverage must bind to the
accepted inventory identifier.

`CollectionRun` public fields: `run_id`, `workspace_id`, `connection_id`,
`provider_channel_id`, `kind`, `status`, `attempt`, `enqueued_at`,
`started_at`, `finished_at`, `pages_fetched`, `quota_spent`, `failure_reason`,
`next_attempt_at`. All values are immutable, UTC, and free of provider text.

Safe failure reasons: `QUOTA_EXHAUSTED`, `PROVIDER_UNAVAILABLE`,
`REAUTH_REQUIRED`, `CANCELLED`, `UNEXPECTED_FAILURE`.

## Quota

Each workspace has a daily budget in provider units, defaulting to 10000, keyed
by workspace and UTC date. Every brokered call deducts the cost the gateway
reported. A run stops before a call it cannot afford.

All three phases (subscribers, video inventory and comments) suspend as `QUEUED`
with `failure_reason=QUOTA_EXHAUSTED`, no finish time and the same run/attempt.
PageCheckpoint retains rows, next page token, visited tokens and usage. The local
workspace budget resumes at UTC midnight, preserving the existing ledger format.
An explicit YouTube `quotaExceeded` / `dailyLimitExceeded` response resumes at
Pacific midnight using `America/Los_Angeles`, including daylight-saving changes.
Rejected API calls count toward usage, never toward fetched pages. Such a response
does not invalidate the owner's Google grant. Local workspace budgets are not
Google's project-wide quota; other clients/workspaces may have consumed it.

No caller may run the queued work before `next_attempt_at`. Completion requires
an exhausted traversal, not a daily limit. Incomplete candidates do not replace
accepted subscriber data; comments still require exact-inventory coverage.

## Slices, suspension, and resuming

`ExecuteRun` carries an optional `slice_seconds`. With one, execution stops at
the deadline and suspends exactly as a quota stop does, except that
`next_attempt_at` is the moment it stopped, so the next caller continues at
once. Without one, execution runs to completion as before.

A suspended run keeps the open collection, inventory, pending video IDs and
page-level checkpoint. The checkpoint contains minimized, deduplicated rows and
an opaque next page token, persisted with collection_jobs v2. Completed per-video
comment candidates remain in channel_data. A restart retains queued checkpoints;
orphan RUNNING jobs still fail closed instead of guessing their commit outcome.

`QUEUED` is deliberately reused for a suspended run rather than a new status:
it is exactly what a suspended run is, and every rule about at most one active
run per connection and kind then applies without a second case.

Two callers drive suspended work, and neither decides policy:

- `execute_due_runs(context, reference_time, slice_seconds)` works every queued
  run of one workspace whose `next_attempt_at` has arrived, offering each at
  most one slice and stopping when the budget is spent.
- `due_workspace_ids(reference_time)` answers which workspaces have a queued run
  ready or an enabled schedule whose interval has arrived, and nothing else
  about them. It takes no context because a caller with no session cannot have
  one; it is therefore the only operation here that is not permission-checked,
  and it returns identifiers a caller must then be separately authorized for.

A driver with no session obtains authority from `workspace-access`, which
issues a context bound to one workspace and carrying one permission. A browser
session is never reused as job authority. For each due workspace the driver
calls `enqueue_due_runs` before `execute_due_runs`, so a schedule becomes a run
under the same least-privilege `collection.run` context that executes it.

### Long waits and cancellation

Quota waiting alone never ends a run after a fixed number of days. No-progress
counts in older ResumePoint records are diagnostic only. A cancelled run clears
its checkpoint and is no longer driven. Repeated/cyclic provider page tokens,
invalid credentials, and other permanent failures still fail closed. Daily quota
waiting is not an unlimited retry of every kind of provider error.

### What a slice is not

A slice is internal execution, not a caller's mutation, and it writes no
idempotency record of its own. The ledger exists so that a caller repeating a
command is answered once; a scheduled driver repeats every minute for as long
as the deployment lives, so a record per slice would grow this module's stored
state without bound and would never be replayed by anyone. What happened is
recorded in the run itself.

Execution also gives the module's lock back at short intervals. One call works
a single run for at most `MAX_LOCK_SLICE_SECONDS` (twenty) even when the caller
offered minutes, because the same process answers people's pages under the same
lock. The caller comes straight back for the rest.

## Retries

A retryable provider failure returns the run to `QUEUED` with `attempt + 1` and
`next_attempt_at = now + backoff(attempt)`, where backoff is 1, 5, and 25
minutes. After three attempts the run is `FAILED` with the last safe reason.
`REAUTH_REQUIRED` is not automatically retried. Quota exhaustion uses the
continuation path above and does not increment the transient retry attempt.

## Schedules

A schedule names one connection and run kind with an interval of at least one
hour. `enqueue_due_runs(context, reference_time)` enqueues one run per due,
enabled schedule that has no active run for the same connection and kind, and
records the enqueue time. Enqueuing is idempotent within an interval window.
If the named connection has since been disconnected, the due evaluation removes
that stale schedule and continues evaluating the remaining schedules. One stale
schedule therefore cannot stop valid work in its workspace or later workspaces.

## Permissions

| Operation | Permission |
|---|---|
| Enqueue, execute, cancel a run | `collection.run` |
| Create, update, delete a schedule | `collection.run` |
| Enqueue due runs | `collection.run` |
| Execute due runs | `collection.run` |
| Ask which workspaces have work waiting | none: names no other fact |
| Read runs and schedules | `collection.read` |
| Workspace cascade | `workspace.delete` |
| Retention purge | `collection.run` |

Every method takes a trusted `WorkspaceContext` and checks the exact permission
itself. A run never carries authority of its own beyond the short-lived
execution authority the broker issues at execution time.

## Stable safe errors

`INVALID_INPUT`, `PERMISSION_DENIED`, `RUN_NOT_FOUND_OR_FORBIDDEN`,
`SCHEDULE_NOT_FOUND_OR_FORBIDDEN`, `RUN_ALREADY_ACTIVE`,
`INVALID_RUN_TRANSITION`, `IDEMPOTENCY_CONFLICT`, `INVALID_CURSOR`,
`CURSOR_EXPIRED`.

Errors expose only a code, a stable message, an optional safe field, a
retryable flag, an opaque correlation id, and an optional safe reason code.
Missing and foreign resources are indistinguishable. Provider responses, quota
internals of the provider, credentials, and stack traces are never public.

## Retention and deletion

| Record | Retention |
|---|---|
| Terminal runs | 90 days |
| Quota ledger entries | 90 days |
| Schedules | until deleted, their connection no longer exists, or workspace cascade |
| Mutation idempotency records | 90 days |

A retention period is a promise about what is no longer kept, and it only holds
if somebody asks: `purge_retention` is a caller's operation, not a background
sweep, and this module runs no timer of its own. The scheduled drain calls it
once per workspace per visit, under the collection authority it already holds
and needing nothing wider. The periods above are unchanged by that; what
changed is that they are now enforced rather than merely stated.


`delete_workspace_jobs` removes schedules, runs, quota ledger entries, cursors,
and idempotency records for one workspace, leaving identically named foreign
records intact. It does not delete `channel-data`; that module owns its own
authorized deletion.

## Testing strategy

Red-green-refactor with a fixed clock, deterministic token source, the real
`channel-connections` service driven by its synthetic fakes, and the real
`channel-data` service. No test contacts a network, browser, database, or real
account. Coverage includes run lifecycle and invalid transitions, quota exact
boundaries, backoff values, cancellation, reauthorization fail-closed
behaviour, schedule due windows, pagination, tenant isolation with identical
identifiers, retention boundaries, and the end-to-end path from provider rows
to a ready silent-analysis dataset.

## Boundaries

### Always

- Resolve the exact permission on a trusted context before any state change.
- Obtain a fresh execution authority per run and never store or log one.
- Keep every key prefixed by workspace, and fail closed on ambiguity.
- Finish the `channel-data` collection exactly once per phase, promoting only
  complete traversals.
- Keep comment data reduced to per-video, per-author counts and latest times.
- Suspend rather than fail when comment coverage runs out of units or time, and
  continue from the recorded point rather than refetching.

### Ask first

- Add a dependency, queue, worker platform, database, or scheduler daemon.
  *Asked and answered: on 2026-08-22 the owner approved a run that resumes and
  a generation that spans days, driven by the browser and by one Cloud Scheduler
  job calling an internal endpoint. No queue, worker platform or daemon was
  added; both drivers call this module's own synchronous methods.*
- Change quota defaults, retry policy, or minimum schedule interval.
- Run against a real Google account or real channel data.

### Never

- Request, store, log, or return a credential, provider payload, or raw error.
- Reuse a browser session as job authority.
- Promote a partial traversal or leave a run stuck in `RUNNING` after a failure.
- Perform real provider calls in this reference slice.

## Success criteria

1. Every operation requires a trusted context and the exact permission.
2. A successful `SUBSCRIBERS` run publishes exactly one accepted snapshot; a
   successful `OWNER_CONTENT` run publishes an inventory and complete coverage.
3. Quota exhaustion during comment coverage suspends the run for the next UTC
   day; in any earlier phase, and for transient failure, cancellation and
   reauthorization, the documented terminal state is produced and prior data is
   left intact.
4. Retries follow the exact 1, 5, 25 minute backoff and stop after three
   attempts.
5. Schedules enqueue at most one active run per connection and kind.
6. Runs, schedules, quota, cursors, and cascades never cross workspaces.
7. Exact retention boundaries are exercised, and no unresolved run is silently
   purged.
8. The end-to-end fixture produces a `channel-data` dataset that
   `analytics-core` classifies into all four segments.
9. A collection larger than one day of units finishes across days without
   refetching a video already covered, and across processes without losing what
   it covered.
10. No dependency, network call, credential, database, queue, or real data is
   introduced.
