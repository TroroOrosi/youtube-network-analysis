# Task List: channel-data

Status: approved
Plan: [`tasks/plan.md`](plan.md)
Spec: [`SPEC-channel-data.md`](../SPEC-channel-data.md)

Complete tasks in order. Every behavior task uses red-green-refactor: add a
focused failing test, confirm the expected failure, implement the smallest
complete behavior, then run focused and regression verification before
committing.

## Task 1: Freeze immutable channel-data contracts

**Description:** Define the smallest public foundation for subscriber,
inventory, activity, collection, pagination, freshness, readiness, retention,
command, error, and repository protocol values.

**Acceptance criteria:**

- [x] Public records are immutable/slotted; enums and error fields have stable
  machine values and no pandas/provider/database/HTTP types leak into them.
- [x] Boundary validation rejects empty/oversized identifiers and text, naive
  times, booleans as counts, invalid counts/ranges, duplicate input keys, and
  forbidden coverage combinations.
- [x] Repository protocols require `WorkspaceContext` for every tenant
  operation and expose the approved permission-specific interfaces.

**Verification:**

- [x] RED then GREEN: `python -m unittest channel_data.tests.test_models -v`
- [x] Workspace and subscriber analytics regression suites pass.
- [x] Compile, public-signature inspection, and `git diff --check` pass.

**Dependencies:** None

**Files likely touched:**

- `channel_data/__init__.py`
- `channel_data/models.py`
- `channel_data/errors.py`
- `channel_data/ports.py`
- `channel_data/tests/test_models.py`

**Estimated scope:** Medium (5 files)

## Task 2: Start tenant-scoped collection intents safely

**Description:** Add the in-memory facade/state seam through one authorized
collection-start path, establishing tenant keys, permission checks, safe lookup,
locking, and actor/payload-bound idempotency before other mutations reuse them.

**Acceptance criteria:**

- [x] Starting a collection requires `collection.run`, stores every key under
  the context workspace, and returns immutable `IN_PROGRESS` state.
- [x] Exact idempotent replay returns the original result; changed actor,
  workspace, operation, or canonical payload fails atomically.
- [x] Identical channel/resource IDs in two workspaces remain independent, and
  missing/foreign public error fields are identical and non-enumerating.

**Verification:**

- [x] RED then GREEN: `python -m unittest channel_data.tests.test_tenant_isolation -v`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Two-workspace runtime fixture, compile, secret scan, and integrity checks
  pass.

**Dependencies:** Task 1

**Files likely touched:**

- `channel_data/__init__.py`
- `channel_data/models.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_tenant_isolation.py`

**Estimated scope:** Medium (5 files)

## Checkpoint A: Contract and tenant boundary

- [x] Tasks 1-2 are independently committed and pushed.
- [x] Contract, permission, idempotency, and identical-ID isolation fixtures
  pass with no foreign existence signal.
- [x] No dependency, persistence, job, OAuth, network, or real data introduced.

## Task 3: Track collection transitions and freshness

**Description:** Add monotonic terminal transition validation and queryable
freshness/history while keeping latest attempt distinct from latest accepted
success; data-bearing complete promotion remains fail-closed until Tasks 4-5
provide a staged candidate.

**Acceptance criteria:**

- [x] Only `IN_PROGRESS -> COMPLETE | PARTIAL | FAILED` is structurally valid;
  terminal replay is idempotent and other transitions fail without mutation.
- [x] Partial/failed attempts retain safe progress/failure codes and never erase
  prior success; `COMPLETE` without the exact staged candidate fails atomically.
- [x] Freshness/history require `collection.read`, order deterministically,
  and expose no provider exception, title, author, payload, or credential data.

**Verification:**

- [x] RED then GREEN:
  `python -m unittest channel_data.tests.test_collection_state -v`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Transition permutation/concurrency fixture, compile, and integrity checks
  pass.

**Dependencies:** Task 2

**Files likely touched:**

- `channel_data/models.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_collection_state.py`

**Estimated scope:** Medium (4 files)

## Task 4: Publish subscriber snapshots and fold registry

**Description:** Stage complete public-subscriber observations, atomically
promote them when the matching collection finishes, and fold them into a
deterministic cumulative tenant registry.

**Acceptance criteria:**

- [x] Candidate staging requires a matching in-progress subscriber attempt,
  mandatory public-only/provider-cap limitations, unique rows, and exposes no
  current snapshot before finish.
- [x] Registry first/last/count/title/published-time rules handle exact replay,
  out-of-order snapshots, and permutations deterministically.
- [x] Exact `COMPLETE` finish promotes candidate plus registry fold atomically;
  absence never marks unsubscribe, partial attempts promote nothing, and an
  identical foreign ID cannot affect counts or lookup.

**Verification:**

- [x] RED then GREEN:
  `python -m unittest channel_data.tests.test_subscribers -v`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Replay/concurrency/permutation fixtures, compile, secret scan, and
  `git diff --check` pass.

**Dependencies:** Task 3

**Files likely touched:**

- `channel_data/models.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_subscribers.py`

**Estimated scope:** Medium (4 files)

## Task 5: Publish videos and fail-closed comment readiness

**Description:** Stage video inventories and per-video author activity,
atomically promote complete coverage, and expose fail-closed silent-analysis
dataset loading.

**Acceptance criteria:**

- [x] Inventory and per-video candidates remain invisible before finish;
  activity accepts positive aggregate rows or explicit empty coverage and never
  retains forbidden raw comment/name/reply fields.
- [x] Coverage is bound to the exact accepted inventory and reaches complete
  only when every listed video, including empty/disabled videos, is covered.
- [x] Exact `COMPLETE` finish atomically promotes inventory/activity only with
  complete coverage; silent loading requires `analysis.read` and fails with
  each stable reason for partial/public/old-inventory coverage.

**Verification:**

- [x] RED then GREEN: `python -m unittest channel_data.tests.test_comments -v`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Empty/disabled/stale/public/partial runtime fixtures, record-field
  allowlist, compile, and integrity checks pass.

**Dependencies:** Task 4

**Files likely touched:**

- `channel_data/models.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_comments.py`

**Estimated scope:** Medium (4 files)

## Checkpoint B: Accepted analysis data

- [x] Tasks 3-5 are independently committed and pushed.
- [x] Partial/failed replacement cannot disturb the last accepted dataset.
- [x] Ready dataset evidence includes exact subscriber limitations and complete
  owner-video coverage for its accepted inventory.

## Task 6: Provide bounded deterministic pagination

**Description:** Add paginated collection-history, snapshot, and registry reads
using opaque server-side continuation state bound to the authorized query.

**Acceptance criteria:**

- [x] Limits enforce 1-500 and fixed datasets follow every approved sort and
  tie-break rule without duplicate or missing rows.
- [x] Cursors are opaque and bound to workspace, query, ordering, and captured
  generation; tampered/cross-workspace/cross-query tokens fail identically.
- [x] Concurrent generation change either preserves the captured result page or
  returns `CURSOR_EXPIRED`; it never silently mixes generations.

**Verification:**

- [x] RED then GREEN across
  `channel_data.tests.test_tenant_isolation` and
  `channel_data.tests.test_collection_state`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Full multi-page traversal/tamper/concurrency fixtures, compile, and
  integrity checks pass.

**Dependencies:** Tasks 4-5

**Files likely touched:**

- `channel_data/models.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_tenant_isolation.py`
- `channel_data/tests/test_collection_state.py`

**Estimated scope:** Medium (5 files)

## Task 7: Enforce retention and deletion cascades

**Description:** Remove expired/superseded data and every in-memory copy during
authorized channel/workspace deletion while preserving other tenants.

**Acceptance criteria:**

- [x] Retention removes superseded video/activity candidates immediately,
  snapshot headers/observations at exactly 365 days, and terminal
  attempts/idempotency records at exactly 90 days without silently deleting
  unresolved in-flight attempts.
- [x] Channel deletion removes data, state, cursors, and idempotency records;
  workspace cascade removes all its channels and no identical foreign record.
- [x] Delete/replay and concurrent publish/delete behavior is atomic,
  non-enumerating, immediately inaccessible, and reports only safe counts.

**Verification:**

- [x] RED then GREEN: `python -m unittest channel_data.tests.test_privacy -v`
- [x] Full channel-data, workspace, and analytics suites pass.
- [x] Exact-cutoff and two-thread cascade fixtures, no-PII/error scan, compile,
  and integrity checks pass.

**Dependencies:** Task 6

**Files likely touched:**

- `channel_data/errors.py`
- `channel_data/memory.py`
- `channel_data/service.py`
- `channel_data/tests/test_privacy.py`

**Estimated scope:** Medium (4 files)

## Checkpoint C: Query and privacy lifecycle

- [x] Tasks 6-7 are independently committed and pushed.
- [x] All tenant copies are covered by retention/cascade tests at exact cutoffs.
- [x] Graph impact/flow/test queries have no parser failure; an unavailable or
  empty graph is recorded rather than misreported as coverage evidence.

## Task 8: Integrate, review, and document channel-data

**Description:** Prove the accepted dataset feeds the existing analytics core,
document the stable public boundary, conduct the full review, and preserve
continuation state for `channel-connections`.

**Acceptance criteria:**

- [x] Fixture mapping reproduces `NEW_SILENT`, `OLD_SILENT`, `DORMANT`,
  and `ACTIVE` plus mandatory public-subscription limitations without changing
  CLI/Notebook results.
- [x] README documents safe usage, permissions, coverage/freshness, pagination,
  retention, deletion, in-memory limits, and exact verification commands.
- [x] Five-axis/security/simplification review has no unresolved Critical or
  Required finding and durable progress identifies verified HEAD/next module.

**Verification:**

- [x] Focused channel-data, workspace, and subscriber analytics suites pass.
- [x] Compile, Notebook code-cell, runtime integration, Markdown fence,
  staged-secret, public-interface, and `git diff --check` checks pass.
- [x] Worktree is clean, all task commits are pushed, and local/upstream HEADs
  match.

**Dependencies:** Task 7

**Files likely touched:**

- `channel_data/README.md`
- `channel_data/tests/test_integration.py`
- `.agents/progress/youtube-analysis-app.md`
- `tasks/todo.md`
- Concrete review-fix file only when required.

**Estimated scope:** Medium (4-5 files)

## Final checkpoint: channel-data complete

- [x] All eight tasks and three intermediate checkpoints are complete.
- [x] Every approved specification success criterion has direct evidence.
- [x] No database, migration, endpoint, OAuth, job, UI, dependency, real
  credential, or real channel data was introduced.
- [x] Branch is pushed and ready to specify `channel-connections`.
