# collection-jobs

Tenant-scoped scheduling and execution of channel collection runs. A run turns
brokered provider rows into accepted `channel-data` generations without ever
holding a credential.

Specification: [`SPEC-collection-jobs.md`](../SPEC-collection-jobs.md)

## What this module is

A deterministic standard-library reference. Execution is **synchronous and
caller driven**: this module owns no thread, timer, queue, worker, or scheduler
daemon. A host driver calls `enqueue_due_runs` and `execute_due_runs`; the
reference web application supplies a scheduler-authenticated drain for that
purpose without moving scheduling policy into the web layer.

No test or code path contacts a network, a real Google account, a database, or
real channel data. The provider is reached only through the
`channel-connections` execution broker, which resolves the credential slot
internally.

## Run kinds

| Kind | What it does |
|---|---|
| `SUBSCRIBERS` | Traverses public subscribers and publishes one accepted snapshot |
| `OWNER_CONTENT` | Publishes the owner video inventory, then covers every video of that exact accepted inventory with per-author comment aggregates |

Owner content runs both phases together because `channel-data` binds comment
coverage to the accepted inventory identifier.

## Lifecycle

```text
QUEUED --execute--> RUNNING --+--> SUCCEEDED
                              +--> QUEUED    (daily quota; same attempt, saved cursor)
                              +--> FAILED    (reauth required, or attempts spent)
                              +--> QUEUED    (transient failure, backoff)
QUEUED | RUNNING --cancel--> CANCELLED
```

Every terminal path finishes the `channel-data` collection exactly once. Only a
complete traversal is promoted; a partial or failed attempt leaves the
previously accepted dataset readable and unchanged.

## Permissions

| Operation | Permission |
|---|---|
| `enqueue_run`, `execute_run`, `cancel_run` | `collection.run` |
| `create_schedule`, `delete_schedule`, `enqueue_due_runs` | `collection.run` |
| `get_run`, `list_runs`, `list_schedules` | `collection.read` |
| `purge_retention` | `collection.run` |
| `delete_workspace_jobs` | `workspace.delete` |

Enqueuing resolves a `CollectionTarget` through the narrow
`channel-connections` target resolver. The resolver requires only
`collection.run` and returns exactly `connection_id` and `provider_channel_id`;
it neither exposes general connection metadata nor requires `channel.read`.

## Quota

A workspace has a daily budget in provider units, defaulting to 10000, keyed by
workspace and UTC date. Before every brokered call the run must be able to
afford a conservative reservation; otherwise it waits as `QUEUED` with
`QUOTA_EXHAUSTED` and `next_attempt_at` at UTC midnight. This applies to subscribers,
video inventory and comments. All keep page checkpoints across process restarts.
The gateway reports the actual cost of each successful call and daily-quota
rejection; the ledger deducts it. Application budgets are workspace scoped, but
YouTube quotas can be shared by all clients of a Google project. Explicit provider
daily exhaustion waits until Pacific midnight, not the local UTC budget reset.
Use system IANA timezone data or the runtime's `tzdata` dependency on Windows/slim
images. Quota waiting does not discard checkpoints after three days.

## Retries

A transient provider failure requeues the run with `attempt + 1` and
`next_attempt_at` set by the exact backoff schedule of 1, 5, and 25 minutes.
Executing before that time fails with a retryable
`INVALID_RUN_TRANSITION`. After three attempts the run is `FAILED`.
Daily quota exhaustion is a continuation, not a new attempt: the existing driver
resumes only after the recorded eligible time. A revoked grant still requires the
owner to reauthorize. A missing grant after a daily wait preserves recorded usage.

See [multi-day collection operations](../docs/operations/multiday-collection.md)
for provider result limits, deployment, and the opt-in subscriber refresh.

## Reads

`list_runs` and `list_schedules` return pages of 1-100 items (default 50)
ordered newest first with an identifier tie-break. Cursors are opaque, single
use, and bound to workspace, query, and the workspace revision captured when the
page was produced. Tampered, foreign, and cross-query tokens share
`INVALID_CURSOR`; a changed result set fails as `CURSOR_EXPIRED`.

## Retention and deletion

| Record | Boundary |
|---|---|
| Terminal runs | 90 days after finishing |
| Quota ledger entries | 90 days after first use |
| Mutation idempotency records | 90 days |
| Schedules whose connection was removed | Removed on the next due evaluation |
| Unresolved (queued or running) runs | never purged automatically |

`delete_workspace_jobs` removes runs, schedules, quota entries, cursors, and
idempotency records for one workspace and leaves foreign records intact. It does
**not** delete `channel-data`; that module owns its own authorized deletion.

## Errors

`INVALID_INPUT`, `PERMISSION_DENIED`, `RUN_NOT_FOUND_OR_FORBIDDEN`,
`SCHEDULE_NOT_FOUND_OR_FORBIDDEN`, `RUN_ALREADY_ACTIVE`,
`INVALID_RUN_TRANSITION`, `IDEMPOTENCY_CONFLICT`, `INVALID_CURSOR`,
`CURSOR_EXPIRED`. Missing and foreign resources are indistinguishable, and no
provider response, credential, or stack trace is ever public.

## Production gates

Worker platform, queue, database, scheduler daemon, distributed locking,
provider SDK and HTTP client, real quota policy tuning, and any real collection
run remain explicit later decisions. The in-memory state here is a reference
implementation, not a durable job store.

## Verification

```powershell
python -m unittest discover -s collection_jobs/tests -v
python -m unittest discover -s channel_connections/tests -v
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q collection_jobs channel_connections channel_data workspace_access subscriber_analytics
git diff --check
```
