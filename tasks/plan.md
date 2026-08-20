# Implementation Plan: workspace-access

Status: approved
Date: 2026-08-20
Specification: [`SPEC-workspace-access.md`](../SPEC-workspace-access.md)
Capability: [`workspace-access`](../CAPABILITY_MAP.md)

## Overview

Implement the approved tenant/session boundary as a standard-library Python
module backed by deterministic in-memory state. The slice proves identity
mapping, secure session handling, workspace selection, role authorization,
membership invariants, export/deletion, retention, and audit redaction without
choosing a database, HTTP framework, authentication provider, or OAuth flow.

## Architecture decisions

- `workspace_access.models` owns immutable public records, commands, stable enum
  values, role permissions, and typed errors.
- `workspace_access.service.WorkspaceAccessService` is the sole orchestration
  interface. Consumers cannot construct authority from a bare workspace ID.
- `workspace_access.ports` contains only the small clock/token/audit boundaries
  needed to make tests deterministic. No framework or persistence type crosses
  the public contract.
- The first adapter is process-local in-memory state protected by one reentrant
  lock. Commands that protect the last owner or claim idempotency execute under
  that lock. A later `channel-data` plan may replace state with atomic storage.
- Session secrets use at least 256 random bits, are returned once with redacted
  `repr`, and only SHA-256 digests are retained. Hashing is safe here because
  tokens are uniformly random high-entropy secrets, not passwords.
- Session authorization is not cached across calls. Context resolution reads
  current user, session, membership, role, and authorization revision.
- All times are timezone-aware UTC. Tests use a fixed clock and deterministic
  token/ID source; no test uses environment variables, network, files, OAuth,
  real users, or production credentials.

## Dependency graph

```text
Immutable contracts and permission matrix
                  |
                  v
       Identity and session lifecycle
                  |
                  v
   Workspace creation/list/context resolution
                  |
                  v
 Membership administration and last-owner safety
                  |
                  v
    Export, deletion, retention, audit redaction
                  |
                  v
        Security and compatibility review
```

## Implementation sequence

### Phase 1: Contract and session foundation

1. Add immutable identifiers, identity/session/workspace records, commands,
   stable errors, closed roles, and the exact permission matrix.
2. Add deterministic ports and session establishment/authentication with secret
   redaction, idle/absolute cutoff handling, revocation, and logout-all.

Checkpoint: focused contract/session tests and the existing analytics suite pass;
no raw secret is retained or rendered.

### Phase 2: Tenant authorization

1. Add atomic workspace creation, stable accessible-workspace listing, explicit
   selection, one-workspace fallback, stale preference fallback, and ambiguous
   selection failure.
2. Add permission resolution into a one-request `WorkspaceContext`, uniform
   missing/foreign errors, and next-request role/revocation effects.

Checkpoint: cross-workspace negative tests demonstrate that no foreign record or
existence signal reaches the caller.

### Phase 3: Membership administration

1. Add owner-only grant, role change, revocation, and self-removal.
2. Claim idempotency keys atomically, reject payload mismatch, and preserve at
   least one owner under concurrent attempts.

Checkpoint: membership tests pass, including two-thread last-owner attempts and
current authorization revision checks.

### Phase 4: Privacy lifecycle and completion

1. Add allowlisted account-access export, sole-owner deletion block, immediate
   session revocation, user tombstoning, retention selection, and secret-free
   audit records.
2. Review graph impact/test coverage, run the security checklist, document the
   public module, update durable progress, and remove only concrete review
   findings within this module.

Final checkpoint: every success criterion in the approved specification is
covered by a focused test or an explicit deferred adapter boundary.

## Verification commands

```powershell
# Focused workspace-access suite
python -m unittest discover -s workspace_access/tests -v

# Existing analytics regression suite
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q workspace_access subscriber_analytics

# Notebook JSON/code-cell syntax validation
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"

# Patch integrity
git diff --check
```

There is no configured formatter, linter, type checker, CI workflow, or native
dependency audit. No dependency is added in this plan.

## Checkpoint policy

- Every behavior uses red-green-refactor; record the expected RED reason before
  adding production code.
- Run the focused suite after each behavior slice and existing analytics tests
  before each code commit.
- Keep contract, sessions, authorization, membership, and privacy changes in
  separate verified commits.
- Use only explicit repository paths when staging. Push only clean, verified
  milestones to `origin/feature/multi-channel-analytics`.
- Update `.agents/progress/youtube-analysis-app.md` after coherent verified
  milestones and before moving to the next capability module.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| In-memory behavior accidentally becomes a production persistence promise | High | Keep persistence explicitly out of public records; specify atomic semantics that a later adapter must preserve |
| Session token appears in repr, audit, or errors | High | Secret wrapper with constant redacted repr plus allowlist tests over every returned/audited record |
| Authorization context stays valid after membership change | High | Resolve current membership per call and reject stale authorization revisions on mutations |
| Guessed IDs reveal cross-workspace existence | High | Same stable errors and tenant-scoped lookup tests for absent and foreign resources |
| Concurrent owner changes leave zero owners | High | Check invariant and mutate under the same lock; exercise two-thread test |
| Broad account deletion damages owned workspaces | High | Fail before mutation when any sole-owned workspace remains; revoke only after preconditions pass |
| Framework concerns leak into domain API | Medium | Public interface accepts typed records only; cookie/CSRF/HTTP enforcement remains a future adapter contract |

## Scope discipline

Intentionally untouched:

- Database schema, migration, ORM, cache, or queue.
- HTTP endpoints, middleware, cookie emission, CSRF library, or UI.
- Passwords, authentication-provider SDKs, invitations, SSO, or account linking.
- Google/YouTube OAuth and connected-channel credentials.
- Background-job authority, channel data, or real user/channel collection.
- Existing CLI/Notebook behavior and dependency manifests.

## Open questions

No blocking questions remain for the approved in-memory slice. Cookie behavior,
authentication provider, database, and background-job authority remain explicit
review gates for their owning modules.
