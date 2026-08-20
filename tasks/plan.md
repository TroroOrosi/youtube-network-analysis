# Implementation Plan: channel-data

Status: proposed for review
Date: 2026-08-20
Specification: [`SPEC-channel-data.md`](../SPEC-channel-data.md)
Capability: [`channel-data`](../CAPABILITY_MAP.md)

## Overview

Implement the approved tenant-scoped channel-data contracts as a deterministic
standard-library Python reference backed by in-memory state. The module will
prove collection lifecycle, immutable generation publication, subscriber
registry folding, video/comment coverage, silent-analysis readiness, bounded
pagination, retention, and deletion without choosing a database, background-job
framework, API, OAuth flow, or production data source.

## Architecture decisions

- `channel_data.models` owns immutable/slotted public records, commands, stable
  enums, pagination values, and validation helpers. It imports no pandas,
  filesystem, provider, database, HTTP, or analytics-core types.
- `channel_data.ports` exposes the four approved repository protocols and
  small deterministic clock/token boundaries. Consumers program to protocols;
  the reference implementation remains replaceable.
- `ChannelDataService` is the public in-memory facade. One reentrant lock
  encloses authorization checks, idempotency claims, state transitions,
  generation publication, cursor snapshots, retention, and cascades.
- `channel_data.memory` holds private typed state and cursor/idempotency
  records. Tenant keys always start with `context.workspace_id`; a resource is
  never fetched globally and authorized afterward.
- Complete subscriber/video generations become visible atomically. A partial
  or failed attempt updates operational state but leaves the last accepted
  dataset readable.
- The specification's `publish_*` commands write candidate data under the
  matching in-progress attempt; they do not switch the current generation.
  `finish_collection(COMPLETE)` validates the complete candidate and promotes
  it under the same lock. Plan approval includes this interface-sequencing
  clarification, which will be copied back into the living spec before code.
- Comment storage is only per-video/per-author count and latest timestamp.
  Complete empty replacements represent comments-disabled/no-comment videos.
  Silent-analysis reads require complete owner-video coverage for the currently
  accepted inventory.
- Pagination uses opaque in-memory cursor tokens bound to workspace, query,
  ordering, and a captured result generation. Missing/tampered/cross-query
  cursors fail safely; the reference makes no restart-survival promise.
- Retention and deletion remove every in-memory copy, including cursor and
  idempotency state. Production transactions, migrations, encryption, backups,
  and automated execution authority remain explicit future gates.

## Dependency graph

```text
Immutable contracts, validation, errors, and protocols
                         |
                         v
          Tenant authorization + idempotency core
                         |
                         v
       Collection attempts, transitions, and freshness
                         |
              +----------+----------+
              |                     |
              v                     v
 Subscriber snapshots/registry   Video inventory/activity
              |                     |
              +----------+----------+
                         v
          Silent readiness + paginated reads
                         |
                         v
           Retention and deletion cascades
                         |
                         v
          Analytics integration + final review
```

## Implementation sequence

### Phase 1: Contract and tenant foundation

1. Add the immutable domain contract, typed safe errors, repository protocols,
   deterministic ports, and intentionally small public export surface.
2. Add the tenant-scoped in-memory facade, exact permission checks,
   non-enumerating lookup behavior, and atomic actor/payload-bound idempotency.

Checkpoint A: contract and isolation tests pass; two workspaces using identical
channel/resource identifiers cannot observe or mutate one another.

### Phase 2: Collection lifecycle and accepted data

3. Add terminal transition validation and freshness/history, proving that
   partial/failed attempts cannot promote a generation and a complete attempt
   without a valid staged candidate fails atomically.
4. Add staged subscriber snapshot candidates, atomic completion/promotion, and
   deterministic cumulative registry folding, including replay and out-of-order
   observations.
5. Add staged video inventory/per-video activity candidates, atomic
   completion/promotion, owner/public coverage, and fail-closed silent-analysis
   dataset loading.

Checkpoint B: partial/failed attempts preserve prior accepted generations;
subscriber and comment fixtures map to a ready dataset only at exact completeness.

### Phase 3: Query and privacy lifecycle

6. Add bounded deterministic pagination with workspace/query/generation-bound
   opaque cursors and explicit invalid/expired behavior.
7. Add exact retention cutoffs, channel deletion, workspace cascade, cursor and
   idempotency cleanup, and concurrent delete/publish isolation.

Checkpoint C: full channel-data, workspace-access, and subscriber analytics
suites pass; retention and deletion leave no addressable tenant copies.

### Phase 4: Integration and completion

8. Map a fixture `SilentAnalysisDataset` into `analytics-core`, reproduce all
   four segments, document public usage and limits, run graph-backed/five-axis
   review, fix only concrete findings, and update durable progress.

Final checkpoint: every approved success criterion has direct evidence and no
database, migration, endpoint, OAuth, job, UI, dependency, real credential, or
real channel data has been introduced.

## Verification commands

```powershell
# Focused channel-data suite
python -m unittest discover -s channel_data/tests -v

# Existing authorization and analytics regressions
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q channel_data workspace_access subscriber_analytics

# Notebook JSON/code-cell validation
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"

# Patch integrity
git diff --check
```

There is no configured formatter, linter, type checker, CI workflow, or native
dependency audit. No dependency is added in this plan. Final review also checks
Markdown fences, staged credential values, public-record mutability, and a
fixture runtime path.

## TDD and checkpoint policy

- Every behavior follows red-green-refactor. Record the expected RED failure
  before writing the corresponding production behavior.
- Run the focused test file after each red/green cycle and the full
  channel-data suite after refactoring.
- Run workspace-access and subscriber analytics regressions before every
  implementation commit.
- Keep contracts, tenant/lifecycle foundation, subscribers, comments,
  pagination, privacy, and final documentation in separate verified commits.
- Build/update the knowledge graph before exploration and after each coherent
  source milestone. If this session's graph remains unregistered/empty, record
  that limitation and use focused local inspection after the required attempt.
- Stage only explicit task paths. Push clean verified milestones to
  `origin/feature/multi-channel-analytics`.
- Update `.agents/progress/youtube-analysis-app.md` after checkpoints and
  before moving to `channel-connections`.

## Definition of Done application

Each task must satisfy its acceptance criteria plus the standing Definition of
Done: focused behavior is runtime-tested, existing suites pass, edge/error
paths are covered, no unrelated changes or duplicate logic remain, public
contracts are documented, integration compatibility is considered, and
security/privacy implications are reviewed. Missing formatter/type-checker/CI
commands are documented project constraints, not silently claimed checks.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Constructed or stale context is treated as permanent authority | High | Require the trusted operation-scoped contract and exact permission on every method; do not serialize contexts or implement jobs |
| Same provider IDs collide across tenants | High | Prefix every resource, cursor, idempotency, and cascade key with workspace and exercise identical-ID cross-tenant fixtures |
| Partial refresh corrupts current analysis | High | Stage by collection/generation and publish under the same lock only after completeness checks |
| Retry double-counts registry/activity | High | Atomically bind idempotency key to workspace, actor, operation, canonical payload fingerprint, and original result |
| Coverage metadata permits false silent classification | High | Require accepted complete subscriber traversal plus complete owner-video coverage for the exact accepted inventory |
| Cursor leaks or crosses tenants/queries | High | Store opaque token state server-side, reapply scope, bind query/generation, and use identical safe invalid behavior |
| Public YouTube identifiers accumulate indefinitely | High | Minimize fields, enforce exact retention/cascade tests, and keep production backup/legal-hold design gated |
| In-memory lock semantics are mistaken for database guarantees | Medium | Document transaction/constraint obligations and keep all persistence artifacts out of this slice |
| Shared facade grows too complex | Medium | Keep state/errors/ports separated and review function/file size after each checkpoint; split only around a proven cohesive seam |

## Scope discipline

Intentionally untouched:

- Database schema, migrations, ORM, cache, queue, object storage, or KMS.
- HTTP/API endpoints, cookies, middleware, background workers, schedules, or UI.
- Google/YouTube OAuth, credentials, provider clients, and real API calls.
- Existing CLI flags, CSV columns, Japanese labels, Notebook controls, and
  analytics-core segment/filter rules.
- Raw comment text, author display names, reply trees, comment IDs, or provider
  payload retention.
- Production data collection, deployment, legal basis, cross-workspace
  administrator erasure, and automated retention authority.

## Open questions

The approved specification left `publish_*` versus
`finish_collection(COMPLETE)` sequencing implicit. This plan resolves it by
staging candidates first and promoting only during successful finish. Human
approval of this plan confirms that clarification together with the task
ordering and checkpoint scope.
