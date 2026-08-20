# Task List: workspace-access

Status: approved
Plan: [`tasks/plan.md`](plan.md)
Spec: [`SPEC-workspace-access.md`](../SPEC-workspace-access.md)

Complete tasks in order. Every behavior task uses red-green-refactor: add a
focused failing test, confirm the expected failure, implement the smallest
change, then run focused and regression verification before committing.

## Task 1: Freeze immutable access contracts

**Description:** Add the smallest public contract foundation: opaque IDs,
verified identity, roles, permissions, sessions, workspaces, memberships,
commands, stable errors, and safe secret wrappers.

**Acceptance criteria:**

- [x] Public records are immutable/slotted and accept only timezone-aware values
  at service boundaries.
- [x] `OWNER` and `MEMBER` resolve to the approved exact permission matrix.
- [x] Session evidence/issued secrets have constant redacted repr/str and errors
  contain stable non-sensitive codes.

**Verification:**

- [x] RED then GREEN: `python -m unittest workspace_access.tests.test_models -v`
- [x] Regression: `python -m unittest discover -s subscriber_analytics/tests -v`
- [x] Compile and `git diff --check` pass.

**Dependencies:** None

**Files likely touched:**

- `workspace_access/__init__.py`
- `workspace_access/models.py`
- `workspace_access/tests/__init__.py`
- `workspace_access/tests/test_models.py`

**Estimated scope:** Medium (4 files)

## Task 2: Establish and revoke secure sessions

**Description:** Implement deterministic identity mapping, one-time session
secret issuance, digest-only retention, authentication, exact expiry cutoffs,
rotation/revocation, logout, and logout-all.

**Acceptance criteria:**

- [x] Verified `(issuer, subject)` maps to one local user without using email as
  authority; disabled/deleted users fail closed.
- [x] Raw secrets are returned once, never stored/audited, and idle/absolute
  limits are evaluated exactly in UTC.
- [x] Revocation is idempotent and a user can revoke only their own sessions.

**Verification:**

- [x] RED then GREEN: `python -m unittest workspace_access.tests.test_sessions -v`
- [x] Full workspace and analytics suites pass.
- [x] Compile, secret-value scan, and `git diff --check` pass.

**Dependencies:** Task 1

**Files likely touched:**

- `workspace_access/models.py`
- `workspace_access/ports.py`
- `workspace_access/service.py`
- `workspace_access/tests/test_sessions.py`

**Estimated scope:** Medium (4 files)

## Checkpoint A: Session boundary

- [x] Tasks 1-2 are independently committed and pushed.
- [x] No dependency, framework, persistence, OAuth, or real data introduced.
- [x] Graph rebuild has no parser error and focused/full tests pass.

## Task 3: Resolve one authorized workspace context

**Description:** Implement workspace creation/listing and fail-closed context
resolution for explicit, implicit, stale, ambiguous, absent, and foreign
workspace selections.

**Acceptance criteria:**

- [x] Workspace creation atomically creates the first owner; accessible lists
  include only current memberships with stable ordering.
- [x] Every successful context contains current role, permissions, session, and
  authorization revision for exactly one workspace.
- [x] Missing and foreign selectors produce the same non-enumerating error and
  no client-supplied authority is trusted.

**Verification:**

- [x] RED then GREEN: `python -m unittest workspace_access.tests.test_authorization -v`
- [x] Full workspace and analytics suites pass.
- [x] Cross-tenant negative runtime fixture, compile, and integrity checks pass.

**Dependencies:** Task 2

**Files likely touched:**

- `workspace_access/models.py`
- `workspace_access/service.py`
- `workspace_access/tests/test_authorization.py`

**Estimated scope:** Medium (3 files)

## Task 4: Enforce membership and last-owner invariants

**Description:** Add owner-only membership grant/change/revoke commands with
idempotency and atomic last-owner protection.

**Acceptance criteria:**

- [ ] Members cannot administer memberships; owners can grant, promote, demote,
  revoke, and self-remove when not the last owner.
- [ ] Same idempotency key/payload replays the result; a changed payload fails
  with `IDEMPOTENCY_CONFLICT`.
- [ ] Serial and concurrent operations cannot leave a workspace with zero
  owners; role/revocation changes affect the next authorization call.

**Verification:**

- [ ] RED then GREEN: `python -m unittest workspace_access.tests.test_memberships -v`
- [ ] Full workspace and analytics suites pass.
- [ ] Two-thread invariant fixture, compile, and integrity checks pass.

**Dependencies:** Task 3

**Files likely touched:**

- `workspace_access/models.py`
- `workspace_access/service.py`
- `workspace_access/tests/test_memberships.py`

**Estimated scope:** Medium (3 files)

## Checkpoint B: Tenant isolation

- [ ] Tasks 3-4 are independently committed and pushed.
- [ ] Permission matrix and negative isolation cases are fully exercised.
- [ ] Graph review finds no unexpected existing-code impact.

## Task 5: Complete privacy and audit lifecycle

**Description:** Implement account-access export, sole-owner deletion blocking,
session revocation/tombstoning, retention selection, and allowlisted audit
events.

**Acceptance criteria:**

- [ ] Export contains only the user's approved account/membership/workspace
  fields and excludes session/provider secrets and other-user PII.
- [ ] Account deletion fails without partial mutation for sole owners; otherwise
  revokes all sessions, removes memberships, and tombstones direct identifiers.
- [ ] Retention selection follows approved 30/90/365-day boundaries and audit
  records contain no raw secrets/assertions/payloads.

**Verification:**

- [ ] RED then GREEN: `python -m unittest workspace_access.tests.test_privacy -v`
- [ ] Full workspace and analytics suites pass.
- [ ] Compile, secret-value scan, and `git diff --check` pass.

**Dependencies:** Task 4

**Files likely touched:**

- `workspace_access/models.py`
- `workspace_access/service.py`
- `workspace_access/tests/test_privacy.py`

**Estimated scope:** Medium (3 files)

## Task 6: Review and document the completed module

**Description:** Run graph-backed and five-axis review, simplify only concrete
findings, document the stable public boundary, and preserve continuation state.

**Acceptance criteria:**

- [ ] README/module docs describe public contracts, safe usage, limitations, and
  exact verification commands without implying production authentication.
- [ ] Graph impact/test queries and security review show no unresolved high-risk
  finding, orphaned public behavior, or missing critical test.
- [ ] Durable progress identifies verified HEAD and the next capability module.

**Verification:**

- [ ] Focused workspace and full analytics suites pass.
- [ ] Compile, Notebook code-cell, secret scan, and `git diff --check` pass.
- [ ] Worktree is clean and verified commits are pushed.

**Dependencies:** Task 5

**Files likely touched:**

- `workspace_access/README.md`
- `.agents/progress/youtube-analysis-app.md`
- `tasks/todo.md`
- Concrete review-fix files only, capped at five files.

**Estimated scope:** Medium (3-5 files)

## Final checkpoint: workspace-access complete

- [ ] All six tasks and both intermediate checkpoints are complete.
- [ ] Approved specification success criteria have verification evidence.
- [ ] No database, endpoint, OAuth, external authentication, dependency, real
  user data, or production behavior was introduced.
- [ ] Branch is pushed and ready to specify `channel-data`.
