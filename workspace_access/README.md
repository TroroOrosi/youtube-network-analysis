# workspace-access

`workspace_access` is the tenant, membership, role, and session boundary for the
future hosted application. The current implementation is a deterministic,
standard-library in-memory reference. It is complete for domain behavior and
tests, but it is not a production authentication or persistence adapter.

The approved contract is [`SPEC-workspace-access.md`](../SPEC-workspace-access.md).

## What it guarantees

- Verified identity authority is the exact `(issuer, subject)` pair. Email is
  optional display/account data and never authorization authority.
- Raw session secrets are returned once, render as `REDACTED`, and are not kept
  by the service. Only SHA-256 digests of uniformly random 256-bit tokens are
  retained.
- Sessions expire after 30 idle minutes or 12 absolute hours. Expiry and
  revocation are fail-closed at the exact cutoff.
- Every tenant action resolves a current membership into one immutable
  `WorkspaceContext`. Client workspace IDs are selectors, never authority.
- Missing and foreign workspace/membership IDs return the same safe error.
- Membership mutations re-read the current role, reject stale authorization
  revisions, and preserve at least one owner under concurrent changes.
- Workspace and membership commands honor idempotency keys and reject key reuse
  with changed payload or actor.
- Account deletion blocks sole owners without partial mutation. Successful
  deletion revokes sessions, removes memberships and direct identifiers, and
  retains only scheduled security/audit tombstones.
- Session records and security/administration audit events follow the approved
  30/90/365-day purge boundaries.

## Roles

| Capability | OWNER | MEMBER |
|---|:---:|:---:|
| Read workspace, channels, collections, and analyses | Yes | Yes |
| Run collections and export analyses | Yes | Yes |
| List memberships | Yes | Yes |
| Update/delete workspace | Yes | No |
| Manage memberships and channel connections | Yes | No |
| Read workspace audit events | Yes | No |

`Role` and `Permission` use stable machine values. UI adapters own localized
labels.

## Minimal fixture usage

```python
from datetime import UTC, datetime

from workspace_access import (
    CreateWorkspace,
    Permission,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceAccessService,
)

access = WorkspaceAccessService()
issued = access.establish_session(
    VerifiedIdentity(
        issuer="https://configured-identity-provider.example",
        subject="verified-provider-subject",
        authenticated_at=datetime.now(UTC),
    )
)

# A future HTTPS/cookie adapter receives issued.secret once. It must not log or
# serialize the secret into application data.
session = access.authenticate_session(SessionEvidence(issued.secret))
context = access.create_workspace(session, CreateWorkspace("Analysis team"))
analysis_context = access.resolve_workspace_context(
    session,
    None,
    Permission.ANALYSIS_READ,
)
assert analysis_context.workspace_id == context.workspace_id
```

Consumers pass `WorkspaceContext` to tenant-scoped repositories. They must scope
the resource lookup by `context.workspace_id` in the same operation; fetching a
resource globally and checking afterward is forbidden.

## Public service behavior

`WorkspaceAccessService` provides:

- session establishment, authentication, logout, own-session revocation,
  logout-all, and trusted security suspension;
- workspace create/list/select/update/delete;
- current-permission context resolution;
- membership grant, role change, and revocation;
- workspace audit listing, account access export/deletion, and retention purge.

All public records are immutable/slotted and all datetimes are timezone-aware
UTC. Failures use `WorkspaceAccessError` with a stable code, safe message, and a
non-secret correlation ID.

## Intentionally absent

- Database/ORM schema, migration, cache, distributed locking, or queue.
- HTTP routes, cookies, CSRF middleware, security headers, CORS, or rate limits.
- Passwords, identity-provider SDKs, invitations, SSO, or account linking.
- Google/YouTube OAuth, refresh-token persistence, KMS, or channel collection.
- Background-job authority or production deployment.

The in-memory adapter loses state on process restart and its lock protects only
one process. Do not use it as hosted production persistence. A later adapter
must preserve the same atomic idempotency, authorization-revision, and
last-owner semantics with database constraints/transactions.

The future browser adapter must use a `__Host-` `Secure`, `HttpOnly`,
`SameSite=Lax` cookie and an independent server-validated CSRF token. It must not
put bearer/provider credentials in browser Web Storage.

## Verification

```powershell
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q workspace_access subscriber_analytics
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"
git diff --check
```

Tests use fixed identities, clocks, and tokens only. They do not contact a
provider, YouTube, a database, or any network service and contain no real user
data or credentials.
