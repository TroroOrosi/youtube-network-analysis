# Spec: channel-data

Status: proposed for review
Date: 2026-08-20
Capability map id: `channel-data`

## Objective

Define the tenant-scoped data contracts that preserve subscriber observations,
subscriber registry history, video inventories, aggregated comment activity,
and collection freshness/coverage for the hosted YouTube analytics application.
Every operation requires a trusted single-workspace `WorkspaceContext`; every
lookup, uniqueness rule, mutation, page cursor, retention action, and deletion
must remain inside that context's workspace.

The module serves future `collection-jobs` writers and `analysis-api` readers.
It makes silent-subscriber analysis possible without duplicating the current
CLI's filesystem layout and without claiming that private subscriptions are
observable. It does not connect a YouTube account, call Google APIs, schedule
work, calculate segments, expose HTTP, render UI, or choose a database.

### User-visible behavior

- A workspace can retain independent data for multiple connected channels.
- The same connected channel reference can exist in another workspace without
  sharing records, cursors, freshness, or deletion state.
- Each successful subscriber collection creates an immutable observation
  snapshot and atomically folds its rows into a cumulative registry.
- The registry preserves the first and last time each publicly visible
  subscriber was observed. Absence from a later snapshot is not treated as an
  unsubscribe event.
- Video inventories and per-video comment activity can be refreshed without
  exposing partial replacement data to analysis readers.
- Every dataset reports collection status, freshness, coverage scope, and
  limitations. A completed API traversal is never described as a complete
  subscriber population.
- Silent-subscriber input is returned only when an accepted subscriber
  snapshot exists and comment coverage is complete for the accepted owner-video
  inventory. Public-video-only or partial comment coverage fails closed.
- Deleting a channel dataset or workspace removes all tenant-owned channel data
  according to the retention and cascade rules below.

## Dependency and trust boundary

`channel-data` depends only on the public contracts of `workspace-access`.
`analytics-core` and `channel-connections` do not become dependencies:

- `channel_id` is an opaque, non-secret reference supplied by the future
  `channel-connections`/fixture adapter. This module does not interpret a
  provider identity or credential.
- Analysis-facing records are converted to `analytics-core` records by the
  future `analysis-api`; `channel-data` does not import calculation types.
- Browser sessions, cookies, serialized contexts, queue payloads, and raw
  workspace IDs are untrusted. Only a context resolved by `workspace-access`
  for the current operation may enter a repository method.
- Background jobs will require separately specified workspace-bound execution
  authority. This spec does not allow a stale browser context to be serialized
  into a job.

Repository methods check the permission carried by the current context:

| Interface | Required permission |
|---|---|
| Collection writes | `collection.run` |
| Collection state/freshness reads | `collection.read` |
| Analysis dataset reads | `analysis.read` |
| Channel dataset deletion | `channel.manage_connection` |
| Workspace data cascade | `workspace.delete` |
| Workspace-scoped retention purge | `channel.manage_connection` |

The MVP roles currently grant these permissions as defined by
`workspace-access`. The repository must not infer authorization from the role
name, user ID, channel ID, or any caller-supplied permission list.

## Threat model

| Threat | Required design response |
|---|---|
| A caller guesses a channel, snapshot, video, or cursor from another workspace | Include `context.workspace_id` in the same storage operation and uniqueness key; missing and foreign resources return the same safe error |
| A forged/stale context or job payload grants access | Accept only the trusted operation-scoped context contract; require the named permission; future jobs must resolve separate current execution authority |
| Partial refresh replaces a usable dataset | Stage one collection generation and atomically publish it only after its completeness invariants pass; readers see the previous accepted generation until then |
| A retry duplicates a snapshot or double-counts registry/comment activity | Claim idempotency within the workspace/channel boundary atomically and bind it to actor, operation, and payload fingerprint |
| Provider data injects malformed values or unsafe text | Treat all collected fields as untrusted; validate identifiers, sizes, counts, and timezone-aware timestamps before mutation; never retain raw payloads |
| Coverage metadata overstates silent-analysis certainty | Keep traversal state, population limitations, video scope, and readiness separate; fail closed for partial or public-video-only comment coverage |
| Public identifiers become unnecessary PII inventory | Store only fields needed for analysis, omit comment text/display names, define retention and complete deletion cascades, and never log row payloads |
| Concurrent collection/deletion exposes mixed tenant state | Serialize each in-memory transaction; future persistence must use constraints/transactions that preserve the same atomic publish and cascade behavior |

## Domain model

All records are immutable/slotted dataclasses. All identifiers are non-empty,
bounded opaque strings. All datetimes are timezone-aware and normalized to UTC
at the public boundary. Counts are non-boolean, non-negative integers.

### Subscriber observations

`SubscriberSnapshot`

- `snapshot_id: str`
- `workspace_id: str`
- `channel_id: str`
- `captured_at: datetime`
- `observed_count: int`
- `traversal_status: COMPLETE`
- `limitations: tuple[CoverageLimitation, ...]`

`CoverageLimitation` initially contains stable machine values:

- `PUBLIC_SUBSCRIPTIONS_ONLY` — private subscriptions are not observable.
- `PROVIDER_RESULT_CAP_POSSIBLE` — provider response limits may omit otherwise
  public subscribers.

`SubscriberObservation`

- `snapshot_id: str`
- `subscriber_channel_id: str`
- `title: str` — bounded display text; not identity authority.
- `api_published_at: datetime | None`

Within one snapshot, `subscriber_channel_id` is unique. Snapshot publication
and all its observations are atomic. `COMPLETE` means the collector exhausted
the available pages for that request; it never means every subscriber was
observable. A partial traversal remains a `PARTIAL` collection attempt with
safe progress counts and never publishes snapshot observations.

`SubscriberRegistryEntry`

- `workspace_id: str`
- `channel_id: str`
- `subscriber_channel_id: str`
- `title: str`
- `api_published_at: datetime | None`
- `first_seen_at: datetime`
- `last_seen_at: datetime`
- `observation_count: int`
- `last_snapshot_id: str`

The registry key is `(workspace_id, channel_id, subscriber_channel_id)`.
Publishing a snapshot atomically creates or updates each observed entry:

- `first_seen_at` remains the earliest accepted `captured_at`;
- `last_seen_at` becomes the latest accepted `captured_at`;
- `observation_count` increases once per distinct accepted snapshot;
- an existing non-empty title and published time may be updated only from the
  newly accepted observation according to one deterministic last-write rule;
- rows absent from the snapshot are unchanged and are not marked unsubscribed.

Replaying the same snapshot with the same payload returns the original result
without changing counts. Reusing its idempotency key or snapshot ID with a
different actor, channel, timestamp, limitations, or rows fails atomically.

### Videos and comment activity

`VideoInventory`

- `inventory_id: str`
- `workspace_id: str`
- `channel_id: str`
- `captured_at: datetime`
- `coverage_scope: OWNER_VIDEOS | PUBLIC_VIDEOS`
- `video_count: int`

`Video`

- `workspace_id: str`
- `channel_id: str`
- `inventory_id: str`
- `video_id: str`
- `title: str`
- `published_at: datetime | None`

Videos are unique by `(workspace_id, channel_id, video_id)`. Replacing an
inventory is staged and atomically published. Readers never observe a mixture
of old and new accepted inventory rows.

`VideoCommentActivity`

- `workspace_id: str`
- `channel_id: str`
- `inventory_id: str`
- `video_id: str`
- `author_channel_id: str`
- `comment_count: int` — positive for a stored row.
- `last_comment_at: datetime`

Rows are aggregated per video and author. The module deliberately does not
store comment text, author display name, parent ID, reply flag, or comment ID;
none is needed to calculate silent subscriber segments. A complete refresh of
one video's activity replaces that video's aggregate rows atomically, including
an empty replacement for comments-disabled or no-comment videos.

`CommentCoverage`

- `inventory_id: str`
- `coverage_scope: OWNER_VIDEOS | PUBLIC_VIDEOS`
- `videos_expected: int`
- `videos_covered: int`
- `videos_missing: int`
- `completed_at: datetime | None`
- `is_complete: bool`

Comment coverage is complete only when every video in the named accepted
inventory has an accepted replacement, including explicit empty replacements.
Coverage for an older inventory cannot make a newer inventory ready.

### Collection state and analysis readiness

`CollectionKind` is `SUBSCRIBERS`, `VIDEOS`, or `COMMENTS`.
`CollectionStatus` is `IN_PROGRESS`, `PARTIAL`, `COMPLETE`, or `FAILED`.

`CollectionState` records one attempt:

- `collection_id`, `workspace_id`, `channel_id`, and `kind`;
- `status`, `started_at`, and optional `completed_at`;
- bounded non-negative progress counts appropriate to the kind;
- optional safe `failure_code` from a closed enum, never a provider response,
  exception message, credential, comment text, or channel title;
- `accepted_generation_id: str | None` naming the generation made visible by a
  successful finalization.

Transitions are monotonic for one attempt:

```text
IN_PROGRESS -> COMPLETE
            -> PARTIAL
            -> FAILED
```

Terminal attempts never return to `IN_PROGRESS`. A later retry starts a new
attempt. Only `COMPLETE` may publish an accepted subscriber snapshot or video
inventory. Comment collection may publish completed per-video replacements
incrementally, but channel-level readiness remains false until all videos in
the selected inventory are covered.

`ChannelDataFreshness` returns the latest attempt and latest accepted success
separately for each collection kind. A failed or partial latest attempt does not
erase the timestamp or generation of the last accepted success.

`SilentAnalysisDataset` contains:

- the cumulative subscriber registry entries;
- comment activity aggregated across the accepted owner-video inventory by
  `author_channel_id` into count and latest-comment time;
- the accepted snapshot/inventory identifiers and capture times;
- subscriber limitations and comment coverage metadata.

It is returned only when all of these are true:

1. a `COMPLETE` subscriber traversal has an accepted snapshot;
2. an accepted video inventory has `OWNER_VIDEOS` scope;
3. comment coverage names that inventory, has owner scope, and is complete;
4. the dataset is not deleted or currently being atomically replaced.

Otherwise the repository raises `DATASET_NOT_READY` with a stable safe reason
code such as `NO_SUBSCRIBER_SNAPSHOT`, `NO_VIDEO_INVENTORY`,
`PUBLIC_VIDEO_SCOPE_ONLY`, or `COMMENTS_INCOMPLETE`. The error contains counts
and opaque correlation data only; it does not return foreign resource details.

## Repository interfaces

The public interfaces are synchronous domain contracts. Async, SQL, ORM, HTTP,
and queue adapters may wrap them later without changing their observable
semantics.

```python
class CollectionWriter(Protocol):
    def start_collection(
        self,
        context: WorkspaceContext,
        command: StartCollection,
    ) -> CollectionState: ...

    def publish_subscriber_snapshot(
        self,
        context: WorkspaceContext,
        command: PublishSubscriberSnapshot,
    ) -> SubscriberSnapshot: ...

    def publish_video_inventory(
        self,
        context: WorkspaceContext,
        command: PublishVideoInventory,
    ) -> VideoInventory: ...

    def replace_video_comment_activity(
        self,
        context: WorkspaceContext,
        command: ReplaceVideoCommentActivity,
    ) -> CommentCoverage: ...

    def finish_collection(
        self,
        context: WorkspaceContext,
        command: FinishCollection,
    ) -> CollectionState: ...


class CollectionReader(Protocol):
    def get_freshness(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> ChannelDataFreshness: ...

    def list_collection_history(
        self,
        context: WorkspaceContext,
        query: CollectionHistoryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[CollectionState]: ...


class AnalysisDataReader(Protocol):
    def load_silent_analysis_dataset(
        self,
        context: WorkspaceContext,
        channel_id: str,
    ) -> SilentAnalysisDataset: ...

    def list_subscriber_registry(
        self,
        context: WorkspaceContext,
        query: SubscriberRegistryQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberRegistryEntry]: ...

    def list_subscriber_snapshots(
        self,
        context: WorkspaceContext,
        query: SubscriberSnapshotQuery,
        page: PageRequest = PageRequest(),
    ) -> Page[SubscriberSnapshot]: ...


class ChannelDataAdministrator(Protocol):
    def delete_channel_data(
        self,
        context: WorkspaceContext,
        command: DeleteChannelData,
    ) -> None: ...

    def delete_workspace_data(
        self,
        context: WorkspaceContext,
        command: DeleteWorkspaceData,
    ) -> None: ...

    def purge_retention(
        self,
        context: WorkspaceContext,
        reference_time: datetime,
    ) -> ChannelDataRetentionReport: ...
```

All mutation commands carry a non-empty bounded `idempotency_key` generated for
one intent and reused across retries. The key is stored with workspace,
channel, actor user, operation, payload fingerprint, state, and result. It is
claimed under the same transaction/lock as the mutation. A key reused with a
different actor or payload raises `IDEMPOTENCY_CONFLICT`; an in-flight duplicate
raises retryable `OPERATION_IN_PROGRESS`. Keys remain for at least 90 days,
longer than the initially proposed collection retry/dead-letter horizon.

`delete_workspace_data` is an internal cascade operation invoked only after
`workspace-access` has authorized the exact workspace deletion and before the
workspace/context disappears. It still requires the matching trusted context
and `workspace.delete`; it never accepts a bare workspace ID from an ingress
adapter. Persistence integration must make the workspace deletion plus
downstream cascade recoverable and must not report success while tenant data
remains addressable. Retention purge is likewise workspace-scoped and requires
the trusted context; future automated cross-workspace retention waits for the
background execution-authority specification.

## Pagination and deterministic ordering

`PageRequest.limit` defaults to 100 and must be between 1 and 500. `cursor` is
optional and opaque. A cursor is a continuation hint, never tenant authority;
each page re-applies `context.workspace_id`, channel, filters, and sort order.
Tampered, expired, cross-workspace, or cross-query cursor use raises
`INVALID_CURSOR` without revealing the referenced resource.

For an unchanged dataset, complete traversal produces each row exactly once in
these orders:

| Result | Order |
|---|---|
| Collection history | `started_at` descending, then `collection_id` ascending |
| Subscriber snapshots | `captured_at` descending, then `snapshot_id` ascending |
| Subscriber registry | `last_seen_at` descending, then `subscriber_channel_id` ascending |
| Videos | `published_at` descending with missing timestamps last, then `video_id` ascending |
| Author activity summary | `author_channel_id` ascending |

Cursor behavior across concurrent accepted-generation changes is explicitly
snapshot-based or returns `CURSOR_EXPIRED`; it must not silently skip or repeat
rows. The in-memory reference may keep cursor state in memory and loses cursors
on restart. A production cursor format requires integrity protection or
server-side state and is part of the future persistence/API review.

## Error semantics

Invalid input or domain state raises `ChannelDataError` with stable `code`, safe
`message`, optional `field`, `retryable`, and a non-secret `correlation_id`.
Initial codes are:

- `INVALID_INPUT`
- `PERMISSION_DENIED`
- `RESOURCE_NOT_FOUND_OR_FORBIDDEN`
- `IDEMPOTENCY_CONFLICT`
- `OPERATION_IN_PROGRESS`
- `INVALID_COLLECTION_TRANSITION`
- `DATASET_NOT_READY`
- `INVALID_CURSOR`
- `CURSOR_EXPIRED`

Missing and cross-workspace identifiers use the identical
`RESOURCE_NOT_FOUND_OR_FORBIDDEN` code/message shape. Errors and logs never
contain titles, author identifiers, comment content, provider payloads,
credentials, idempotency payloads, storage exceptions, or stack traces.

## Privacy, retention, export, and deletion

### Data classification and minimization

- Workspace/channel/snapshot/inventory/collection IDs are tenant operational
  metadata.
- Subscriber and comment-author channel IDs, display titles, and timestamps are
  public-source personal identifiers and are treated as PII for access,
  retention, export, and deletion design.
- Comment bodies, author display names, parent/reply relationships, raw provider
  payloads, IP addresses, credentials, and OAuth identity are not collected by
  this module.
- Audit/telemetry records contain opaque counts, result codes, durations, and
  correlation IDs only; they never copy row data.

### Proposed retention schedule

| Data | Retention requirement |
|---|---|
| Current subscriber registry and accepted video/comment aggregates | While the channel dataset is active and needed for workspace analysis |
| Subscriber snapshot headers and observations | 365 days after `captured_at` |
| Superseded video/comment generations | Remove immediately after atomic replacement; no history copy by default |
| Terminal collection-attempt history | 90 days after completion |
| Active/in-flight collection state | Until terminal resolution; stale attempts require an explicit recovery decision, never silent deletion |
| Mutation idempotency records | At least 90 days after terminal result |
| Deleted channel/workspace active records | Immediately inaccessible; physically purge within 30 days, subject only to documented legal/security holds and deployment backup expiry |

Exact regional legal bases, holds, backup erasure, and production storage
encryption require deployment/privacy review before real data persistence. A
hold must have owner, reason, scope, and expiry; indefinite retention is not a
default.

### Export and cascade obligations

- A future workspace export may include the workspace's current registry,
  accepted coverage metadata, and aggregate activity, subject to authorization
  and output minimization defined by `analysis-api`.
- App-account deletion alone does not delete shared workspace data when the
  user is not deleting the workspace. It removes that user's access through
  `workspace-access`; public YouTube author data is not joined to app identity
  by this module.
- Channel dataset deletion removes its snapshots, observations, registry,
  inventories, videos, activity, collection state/history, cursors, and
  idempotency records in one recoverable cascade.
- Workspace deletion cascades every channel dataset in that workspace and must
  not touch the same channel reference stored in another workspace.
- A data-subject erasure workflow for a public YouTube author identifier must be
  able to locate and delete that identifier across registry, snapshots, and
  aggregate activity within the authorized workspace(s). Cross-workspace
  erasure authority and legal verification are not invented here and require a
  later privacy/API specification.

## Tech stack

- Python 3.12.
- Standard-library `dataclasses`, `datetime`, `enum`, `hashlib`, `secrets`,
  `threading`, and `typing.Protocol` for contract and fixture implementation.
- Existing `unittest` test runner; no new dependency for this module.
- `workspace_access.models.WorkspaceContext` and `Permission` are the only
  required project-module imports.

A database, ORM, migration tool, cache, queue, encryption/KMS service, Web
framework, and provider SDK remain undecided. Before persistence is introduced,
a reviewed adapter design must map every tenant uniqueness rule, atomic
generation publication, idempotency claim, cursor behavior, retention index,
and deletion cascade to concrete constraints and transactions. No migration is
created under this specification alone.

## Commands

```powershell
# Focused channel-data suite (after implementation approval)
python -m unittest discover -s channel_data/tests -v

# Workspace boundary and analytics regressions
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q channel_data workspace_access subscriber_analytics

# Notebook code-cell validation
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"

# Whitespace and patch integrity
git diff --check
```

There is no configured formatter, linter, type checker, or CI workflow. Adding
one is outside this module and requires a separate reviewed change.

## Proposed project structure

No source directory is created while this specification is under review. After
approval, the implementation plan may propose:

```text
channel_data/
  __init__.py                    # Intentionally small public export surface
  models.py                      # Immutable records, commands, enums, pages
  errors.py                      # Stable safe error constructors
  ports.py                       # Repository protocols and clock/token ports
  memory.py                      # Deterministic fixture/reference repository
  service.py                     # Permission, transaction, and lifecycle facade
  tests/
    test_models.py               # Validation and immutable contracts
    test_tenant_isolation.py     # Context, permission, lookup, cursor boundaries
    test_subscribers.py          # Snapshot publication and registry folding
    test_comments.py             # Inventory/activity replacement and readiness
    test_collection_state.py     # Transitions, freshness, retries, concurrency
    test_privacy.py              # Retention, cascade, redaction, minimization
```

The public facade may be split only when the plan demonstrates a smaller stable
interface. Persistence adapters, migrations, provider clients, API routes, and
background jobs are not placed in this package during the fixture slice.

## Code style

Use immutable values, explicit UTC normalization, record-based commands, and
interfaces that make tenant scope impossible to omit:

```python
@dataclass(frozen=True, slots=True)
class PublishSubscriberSnapshot:
    channel_id: str
    collection_id: str
    snapshot_id: str
    captured_at: datetime
    traversal_status: SubscriberTraversalStatus
    limitations: tuple[CoverageLimitation, ...]
    observations: tuple[SubscriberObservationInput, ...]
    idempotency_key: str


def publish_subscriber_snapshot(
    context: WorkspaceContext,
    command: PublishSubscriberSnapshot,
) -> SubscriberSnapshot:
    ...
```

No tenant repository method accepts `workspace_id` as a substitute for
`WorkspaceContext`. Input and output types are separate. Helpers remain private
unless a second consumer needs a stable contract. Public values do not expose
pandas, filesystem paths, JSON dictionaries, Google clients, SQL rows, or
provider response objects.

## Testing strategy

Implementation follows red-green-refactor. Tests use the real
`WorkspaceAccessService` with fixed identities to obtain contexts, plus fixed
clocks/tokens and an in-memory channel-data service. No test contacts a network,
database, provider, or real account.

- Contract tests cover immutability, slots, stable enums/errors, UTC
  normalization, identifier/length/count validation, and malformed external
  values including booleans masquerading as integers.
- Subscriber tests cover atomic publication, duplicate-row rejection,
  first/last/count folding, title/published-time update policy, exact replay,
  conflicting replay, out-of-order capture times, and absence-without-unsubscribe.
- Inventory/activity tests cover atomic generation visibility, empty videos,
  comments-disabled videos, per-video replacement, author aggregation, stale
  inventory rejection, and every silent-readiness failure reason.
- Tenant tests create two workspaces containing identical channel/video/author
  identifiers and prove reads, writes, cursors, idempotency, and deletion never
  cross the workspace boundary. Missing and foreign errors are byte-for-byte
  equivalent in their public fields.
- Permission tests cover collection, analysis, state-read, and deletion
  permissions without relying on role names.
- Collection-state tests cover every valid and invalid transition, latest
  attempt versus latest accepted success, partial/failed preservation of the
  prior dataset, concurrent duplicate publication, and deterministic history.
- Pagination tests cover limits, stable tie-breaking, complete traversal,
  cursor/query/workspace binding, tampering, expiry, and concurrent-generation
  behavior.
- Privacy tests prove no forbidden comment fields exist, no PII or secret enters
  errors/audit state, exact 90/365-day retention boundaries, channel cascade,
  and workspace cascade isolation.
- Integration tests map `SilentAnalysisDataset` into `analytics-core` fixture
  records and reproduce all four segments, including `NEW_SILENT` and
  `OLD_SILENT`, without changing CLI or Notebook behavior.
- Mutation and permutation tests prove caller inputs remain unchanged and
  equivalent row order yields identical accepted records and pages.

Each coherent slice runs the focused suite plus workspace/analytics regressions
before commit. Final verification also compiles every Notebook code cell,
checks Markdown fences and whitespace, and scans staged changes for credential
values.

## Boundaries

### Always

- Resolve current workspace authority before the repository call and pass one
  trusted `WorkspaceContext` to every tenant operation.
- Include `context.workspace_id` in the same storage read/write, uniqueness
  constraint, cursor binding, idempotency record, and deletion operation.
- Validate all collector/provider-derived data as untrusted at the command
  boundary and normalize aware datetimes to UTC.
- Publish complete generations atomically and keep the last accepted dataset
  readable while a new attempt is partial or failed.
- Preserve public-subscription and provider-cap limitations in every subscriber
  dataset and fail closed for incomplete silent-analysis coverage.
- Minimize row data and apply retention/deletion to every copy, including
  cursors, idempotency records, caches, indexes, exports, and backups.
- Run focused and regression verification before every implementation
  checkpoint.

### Ask first

- Add or upgrade a dependency.
- Choose or change a database, ORM, migration system, cache, queue, Web
  framework, object store, KMS/encryption service, or deployment environment.
- Persist real subscriber/comment data or introduce production credentials.
- Change retention periods, export/erasure semantics, coverage readiness, or
  the public permission matrix.
- Add a new category of personal data, raw provider payload, comment text,
  display name, IP/security metadata, or cross-workspace operation.
- Change existing CLI flags, CSV columns, Japanese labels, Notebook controls,
  or analytics-core segment rules.

### Never

- Call YouTube, start OAuth, read credentials/environment secrets, or perform
  network I/O.
- Infer private subscribers, an unsubscribe event from absence, or a complete
  subscriber total from a completed traversal.
- Fetch a resource globally and authorize it afterward.
- Trust a bare workspace/user/channel/role/permission/cursor value as authority.
- Expose a partial generation as current or run silent analysis over partial or
  public-video-only comment coverage.
- Store comment text, author display names, raw provider payloads, OAuth values,
  or secrets; log subscriber/author row data.
- Mutate caller-owned inputs or return mutable internal collections.

## Success criteria

1. Every public tenant operation requires a trusted `WorkspaceContext` and a
   named permission; no interface can substitute a bare workspace ID.
2. All resource lookup, uniqueness, cursor, idempotency, retention, and deletion
   behavior is workspace-scoped and has cross-workspace negative tests.
3. Subscriber snapshots publish atomically and replay safely; registry
   first/last/count semantics are deterministic and never infer unsubscribe.
4. Subscriber coverage always includes `PUBLIC_SUBSCRIPTIONS_ONLY` and
   `PROVIDER_RESULT_CAP_POSSIBLE`; traversal completion is not population
   completeness.
5. Video inventories and per-video activity replacements are generation-bound
   and atomic; partial/failed refreshes preserve the previous accepted dataset.
6. Stored comment activity contains only video ID, author channel ID, positive
   count, and latest timestamp—no comment text, author name, reply/parent data,
   comment ID, or raw payload.
7. Silent analysis input fails closed until owner-video comment coverage is
   complete for the currently accepted inventory, then maps losslessly to
   `analytics-core` records and reproduces all four segments.
8. Collection state distinguishes latest attempt, latest accepted success,
   traversal completeness, population limitations, scope, freshness, and
   analysis readiness.
9. List methods have bounded pagination, explicit deterministic tie-breakers,
   workspace/query-bound cursors, and tested concurrent-generation behavior.
10. Stable typed errors reveal no foreign-resource existence, row PII,
    credentials, provider payloads, or internal exceptions.
11. The proposed 90/365-day retention boundaries, immediate logical deletion,
    30-day physical purge obligation, account semantics, channel cascade, and
    workspace cascade are tested at exact cutoffs.
12. The implementation is a deterministic standard-library in-memory reference
    only; no database, migration, endpoint, OAuth, job, UI, dependency, real
    credential, or real channel data is introduced.
13. Focused, workspace-access, subscriber analytics, compile, Notebook-cell,
    secret-scan, Markdown, and `git diff --check` verification all pass.

## Review decisions requested

Please confirm these intentional contract choices before planning or code:

1. `channel_id` is an opaque connection reference and is always unique/scoped
   with `workspace_id`; this module does not own provider connection identity.
2. Completed subscriber traversal still carries public-only and possible-cap
   limitations and is never presented as the complete subscriber population.
3. Comment storage is reduced to per-video/per-author aggregates; comment IDs,
   text, names, and reply relationships are deliberately discarded.
4. Silent analysis requires complete comment coverage for an owner-visible
   inventory; public-video-only coverage is rejected rather than merely warned.
5. Subscriber observations are retained for 365 days, terminal attempt/history
   and idempotency records for at least 90 days, and superseded comment/video
   generations are removed immediately after atomic replacement.
6. App-account deletion removes access but preserves shared workspace data;
   channel/workspace deletion performs the data cascade.
7. Persistence schemas and migrations remain gated until these repository,
   tenant, retention, and atomicity contracts are approved.
