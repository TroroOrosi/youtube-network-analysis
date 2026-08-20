# Task List: channel-connections

Status: proposed
Plan: [`tasks/plan.md`](plan.md)
Spec: [`SPEC-channel-connections.md`](../SPEC-channel-connections.md)

Complete tasks in order. Every behavior task uses red-green-refactor: add a
focused failing test, confirm the expected failure, implement the smallest
complete behavior, then run focused and regression verification before
committing.

## Task 1: Freeze immutable connection contracts

**Description:** Define the smallest public foundation for connection,
authorization-start, command, page, audit, retention, and error values plus the
internal redacted secret, provider credential, and verified grant types and
every port protocol.

**Acceptance criteria:**

- [x] Public records are immutable/slotted; `ConnectionProvider`,
  `ConnectionStatus`, audit actions, and the twelve error codes have stable
  machine values, and no HTTP, provider SDK, database, or analytics type leaks
  into them.
- [x] Boundary validation rejects empty/oversized identifiers, titles, callback
  values, and idempotency keys, naive datetimes, out-of-range page limits, and
  unknown enum values; datetimes normalize to UTC.
- [x] `RedactedSecret`, `ProviderCredential`, and `VerifiedProviderGrant` stay
  out of the package root, always render redacted in `str`/`repr`, and no
  public protocol exposes a credential-returning method.
- [x] `ConnectionReader`, `ConnectionManager`,
  `ConnectionPrivacyAdministrator`, `Clock`, `TokenGenerator`,
  `EphemeralSecretStore`, `CredentialVault`, `YouTubeAuthorizationGateway`, and
  the audit sink require `WorkspaceContext` on every tenant operation.

**Verification:**

- [x] RED then GREEN: `python -m unittest channel_connections.tests.test_models -v`
- [x] Workspace-access, channel-data, and subscriber analytics regression
  suites pass.
- [x] Compile, public-signature inspection, redacted-rendering check, and
  `git diff --check` pass.

**Dependencies:** None

**Files likely touched:**

- `channel_connections/__init__.py`
- `channel_connections/models.py`
- `channel_connections/errors.py`
- `channel_connections/ports.py`
- `channel_connections/tests/test_models.py`

**Estimated scope:** Medium (5 files)

## Task 2: Start bound one-time authorization intents

**Description:** Add the in-memory facade/state seam through
`begin_authorization`, establishing tenant keys, permission checks, safe
lookup, locking, actor/payload-bound idempotency, state digest indexing, PKCE
S256, and fixed provider configuration before any other operation reuses them.

**Acceptance criteria:**

- [x] `begin_authorization` requires `channel.manage_connection`, binds
  workspace, user, session, operation, redirect URI identifier, and provider
  from trusted state, and returns only `intent_id`, the gateway authorization
  URL, and `expires_at`.
- [x] The intent stores a state digest rather than the raw state, holds its
  PKCE verifier in the ephemeral store with a S256 challenge, requests exactly
  `youtube.readonly`, and expires at exactly 10 minutes.
- [x] Exact idempotent replay returns the original unexpired result; changed
  actor, workspace, session, operation, or canonical payload fails atomically,
  and one idempotency key creates at most one intent under concurrency.
- [x] The command cannot influence provider host, scope, redirect URI, return
  route, workspace, or user, and a member without the permission is denied.

**Verification:**

- [x] RED then GREEN:
  `python -m unittest channel_connections.tests.test_authorization -v`
- [x] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [x] Two-workspace runtime fixture, compile, secret scan, and integrity checks
  pass.

**Dependencies:** Task 1

**Files likely touched:**

- `channel_connections/models.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_authorization.py`

**Estimated scope:** Medium (4 files)

## Task 3: Complete callbacks and publish verified connections

**Description:** Add `complete_authorization` with the ten approved callback
gates, the strict fake gateway, deterministic credential slots, atomic
publication, and bounded exact replay.

**Acceptance criteria:**

- [x] Completion hashes the raw state, requires an unexpired intent, resolves a
  fresh `channel.manage_connection` context, and fails identically for missing,
  expired, foreign, or wrongly bound workspace/user/session values.
- [x] The intent is claimed atomically before any exchange; an in-flight
  duplicate receives a stable retryable conflict and the gateway records
  exactly one exchange under concurrency.
- [x] Publication requires a refresh token, exactly the approved scope set,
  exactly one `mine=true` channel, and a successful `mySubscribers=true` probe;
  zero/multiple channels, extra scopes, missing refresh token, malformed
  provider values, and probe failure each fail closed with a safe code and
  revoke the grant.
- [x] The active uniqueness key and credential slot are reserved atomically
  before visibility, a duplicate active provider channel fails as
  `CONNECTION_ALREADY_EXISTS`, and a failed vault put releases the reservation
  and leaves explicit cleanup state.
- [x] Ephemeral secrets are deleted on success, denial, and error; the consumed
  intent retains only a secret-free replay result, exact duplicate callbacks
  return the original connection without a second exchange, and changed
  actor/session/digest/operation/payload fails as `CALLBACK_CONFLICT`.
- [x] No public value, error, or audit event contains a token, code, state,
  verifier, vault slot, provider payload, provider error text, or candidate
  channel list.

**Verification:**

- [x] RED then GREEN across
  `channel_connections.tests.test_authorization` and
  `channel_connections.tests.test_connections`
- [x] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [x] Concurrency, denial, malformed-provider, and representation-inspection
  fixtures, compile, secret scan, and `git diff --check` pass.

**Dependencies:** Task 2

**Files likely touched:**

- `channel_connections/models.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_authorization.py`
- `channel_connections/tests/test_connections.py`

**Estimated scope:** Large (5 files)

## Checkpoint A: Contract and authorization boundary

- [x] Tasks 1-3 are independently committed and pushed.
- [x] Contract, permission, intent-binding, one-time claim, provider
  verification, and replay fixtures pass with no foreign existence signal.
- [x] No dependency, Web route, real provider call, persistence, credential, or
  real data introduced.

## Task 4: Read connections with bounded pagination

**Description:** Add `get_connection` and `list_connections` using opaque
server-side cursors bound to the authorized query.

**Acceptance criteria:**

- [ ] Reads require `channel.read`, so a member can see safe metadata; missing
  and foreign connections share
  `CONNECTION_NOT_FOUND_OR_FORBIDDEN`.
- [ ] Limits enforce 1-100 with default 50, ordering is `connected_at`
  descending then `connection_id` ascending, and full traversal returns no
  duplicate or missing row.
- [ ] Cursors are opaque, bound to workspace, ordering, limit, and the captured
  result; tampered, reused, cross-workspace, and cross-query tokens fail as
  `INVALID_CURSOR` while superseded captures fail as `CURSOR_EXPIRED`, and no
  cursor contains provider metadata or credentials.

**Verification:**

- [ ] RED then GREEN:
  `python -m unittest channel_connections.tests.test_connections -v`
- [ ] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [ ] Multi-page traversal, tamper, and concurrent-change fixtures, compile,
  and integrity checks pass.

**Dependencies:** Task 3

**Files likely touched:**

- `channel_connections/models.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_connections.py`

**Estimated scope:** Medium (4 files)

## Task 5: Reauthorize and rotate credentials atomically

**Description:** Add `begin_reauthorization` and its completion path through
the same intent, state, PKCE, scope, ownership, and capability gates with exact
provider channel matching.

**Acceptance criteria:**

- [ ] `begin_reauthorization` requires `channel.manage_connection` and an
  existing connection in the current workspace; a missing or foreign target
  fails non-enumerably.
- [ ] A verified provider channel that differs from the target fails as
  `REAUTH_CHANNEL_MISMATCH` without replacing, deleting, or invalidating the
  current credential.
- [ ] A successful reauthorization rotates the credential slot, updates the
  connection atomically from a reader's perspective, deletes the old slot only
  after the new revision commits, and returns `REAUTH_REQUIRED` to `ACTIVE`.
- [ ] Failed old-slot cleanup is tracked by a secret-free cleanup identifier
  for retry and never by copied token material.

**Verification:**

- [ ] RED then GREEN:
  `python -m unittest channel_connections.tests.test_connections -v`
- [ ] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [ ] Rotation, mismatch, recovery, and concurrent reauthorization fixtures,
  compile, secret scan, and integrity checks pass.

**Dependencies:** Task 4

**Files likely touched:**

- `channel_connections/models.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_connections.py`

**Estimated scope:** Medium (4 files)

## Task 6: Disconnect, revoke, and fail closed on invalidation

**Description:** Add fail-closed disconnect ordering, provider-revocation
uncertainty handling, and the `report_credential_invalidation` seam approved by
the plan.

**Acceptance criteria:**

- [ ] Disconnect requires `channel.manage_connection` and follows the approved
  order: mark unavailable, request revocation, delete the credential slot
  regardless of revocation confirmation, remove metadata and the active
  uniqueness key, and retain only secret-free evidence.
- [ ] An unconfirmed or failed provider revocation is recorded as a secret-free
  cleanup outcome, no token is retained for retry, and exact disconnect replay
  is idempotent while a changed payload conflicts.
- [ ] `report_credential_invalidation` deletes the slot, publishes
  `REAUTH_REQUIRED`, emits `CONNECTION_REAUTH_REQUIRED`, accepts no provider
  payload or token, and leaves no usable credential.
- [ ] Disconnecting the same provider channel frees the active key for a new
  connection ID, does not delete `channel-data`, and racing disconnect,
  connect, and reauthorization never overwrite or resurrect a credential slot.

**Verification:**

- [ ] RED then GREEN:
  `python -m unittest channel_connections.tests.test_connections -v`
- [ ] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [ ] Revocation-uncertainty, replay, invalidation, and two-thread race
  fixtures plus a `channel-data` non-deletion check pass.

**Dependencies:** Task 5

**Files likely touched:**

- `channel_connections/errors.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_connections.py`

**Estimated scope:** Medium (4 files)

## Checkpoint B: Connection lifecycle

- [ ] Tasks 4-6 are independently committed and pushed.
- [ ] Rotation, disconnect, and invalidation leave no usable or orphaned
  credential slot.
- [ ] Collected `channel-data` remains intact after disconnect.

## Task 7: Prove tenant isolation across every operation

**Description:** Exercise identical connection, provider channel, intent,
state, cursor, secret-slot, and idempotency identifiers in two workspaces
across reads, authorization, callback, reauthorization, disconnect, and
administration, and fix any leak the fixtures expose.

**Acceptance criteria:**

- [ ] The same provider channel is independently connectable in two workspaces
  and neither connection, credential, intent, cursor, idempotency record, nor
  audit event is readable, replayable, or mutable from the other.
- [ ] A callback bound to one workspace cannot be completed from the other even
  with an identical state value, and foreign identifiers produce byte-identical
  safe errors.
- [ ] A cascade, retention purge, disconnect, or invalidation in one workspace
  leaves the identically named foreign records untouched.

**Verification:**

- [ ] RED then GREEN:
  `python -m unittest channel_connections.tests.test_tenant_isolation -v`
- [ ] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [ ] Identical-ID cross-tenant fixtures, compile, secret scan, and integrity
  checks pass.

**Dependencies:** Task 6

**Files likely touched:**

- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_tenant_isolation.py`

**Estimated scope:** Medium (3 files)

## Task 8: Enforce retention, cascade, and audit safety

**Description:** Add exact retention cutoffs, orphan secret reconciliation,
workspace cascade, secret-free audit events, and export representation
inspection.

**Acceptance criteria:**

- [ ] `purge_retention` requires `channel.manage_connection` and removes
  unclaimed intents and PKCE slots at exactly 10 minutes, consumed replay
  records at exactly 24 hours, and terminal idempotency and resolved cleanup
  records at exactly 90 days without deleting unresolved operations.
- [ ] Orphan ephemeral and unreferenced credential slots are reconciled without
  reading secret material, and the report exposes only safe counts.
- [ ] `delete_workspace_connections` requires `workspace.delete` and removes
  connections, intents, cursors, replay and idempotency records, cleanup state,
  ephemeral secrets, and credential slots for that workspace only, with
  idempotent replay.
- [ ] Every audit event contains only actor, workspace, action, opaque
  target/intent ID, outcome, timestamp, and correlation ID; export inspection
  finds no credential slot, digest, provider error, or scope beyond the fixed
  product constant.

**Verification:**

- [ ] RED then GREEN:
  `python -m unittest channel_connections.tests.test_privacy -v`
- [ ] Full channel-connections, workspace-access, channel-data, and analytics
  suites pass.
- [ ] Exact-cutoff, orphan-reconciliation, two-thread cascade, and
  no-secret representation fixtures, compile, and integrity checks pass.

**Dependencies:** Task 7

**Files likely touched:**

- `channel_connections/models.py`
- `channel_connections/memory.py`
- `channel_connections/service.py`
- `channel_connections/tests/test_privacy.py`

**Estimated scope:** Medium (4 files)

## Checkpoint C: Isolation and privacy lifecycle

- [ ] Tasks 7-8 are independently committed and pushed.
- [ ] Retention and cascade tests cover every exact cutoff and every secret
  store copy.
- [ ] Graph impact/flow/test queries have no parser failure; an unavailable or
  empty graph is recorded rather than misreported as coverage evidence.

## Task 9: Integrate, review, and document channel-connections

**Description:** Prove a ready connection resolves through real
`workspace_access` contexts for a `channel-data` consumer, document the stable
public boundary, conduct the full review, and preserve continuation state.

**Acceptance criteria:**

- [ ] An integration fixture resolves real owner and member contexts, publishes
  a ready connection, exposes it to a `channel-data` style consumer by opaque
  connection and provider channel ID only, and changes no CLI, Notebook, or
  analytics behavior.
- [ ] README documents safe usage, the permission matrix, the authorization and
  callback lifecycle, reauthorization, disconnect versus data deletion,
  retention boundaries, in-memory fake limits, production vault/KMS gates, and
  exact verification commands.
- [ ] Five-axis, security, and simplification review has no unresolved Critical
  or Required finding, and durable progress records the verified HEAD and the
  next module.
- [ ] The living specification is updated with the two approved
  interface-sequencing clarifications.

**Verification:**

- [ ] Focused channel-connections, workspace-access, channel-data, and
  subscriber analytics suites pass.
- [ ] Compile, Notebook code-cell, runtime integration, Markdown fence,
  staged-secret, public-interface, and `git diff --check` checks pass.
- [ ] Worktree is clean, all task commits are pushed, and local/upstream HEADs
  match.

**Dependencies:** Task 8

**Files likely touched:**

- `channel_connections/README.md`
- `channel_connections/tests/test_integration.py`
- `SPEC-channel-connections.md`
- `.agents/progress/youtube-analysis-app.md`
- `tasks/todo.md`
- Concrete review-fix file only when required.

**Estimated scope:** Medium (5-6 files)

## Final checkpoint: channel-connections complete

- [ ] All nine tasks and three intermediate checkpoints are complete.
- [ ] Every approved specification success criterion has direct evidence.
- [ ] No Web route, real provider call, SDK, database, migration, job, UI,
  dependency, real credential, or real channel data was introduced.
- [ ] Branch is pushed and ready to specify the hosted API/UI slice.
