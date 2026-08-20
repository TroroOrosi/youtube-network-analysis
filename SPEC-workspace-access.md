# Spec: workspace-access

Status: proposed for review
Date: 2026-08-20
Capability map id: `workspace-access`

## Objective

Define the identity, workspace membership, role, session, tenant-selection, and
authorization contracts for the hosted YouTube analytics application before a
database, Web framework, or authentication provider is chosen. Every later
tenant-scoped module must receive one trusted workspace context and must fail
closed when that context cannot be established.

This module serves workspace owners, workspace members, browser/API adapters,
and the future `channel-data`, `channel-connections`, `collection-jobs`, and
`analysis-api` modules. It does not authenticate passwords, implement OAuth,
store records, expose HTTP routes, or render UI.

### User-visible behavior

- A person can have one user account and memberships in multiple workspaces.
- A workspace has one or more owners and zero or more members.
- An authenticated user sees and selects only workspaces where a current
  membership exists.
- Role changes and membership revocation take effect on the next authorization
  decision, without waiting for the browser session to expire.
- A member cannot discover another workspace or its resources by guessing an
  identifier.
- Logging out, session expiry, account suspension/deletion, and explicit
  revocation stop future use of the affected session.
- Account data can be exported and deleted subject to workspace-ownership and
  narrowly defined security-retention constraints.

## Domain model

All identifiers are opaque, stable, non-secret values. They carry no role,
email address, workspace name, or sequential business meaning.

- **User** — the local account mapped from a verified external identity. A user
  is global and is not itself owned by a workspace.
- **Verified identity** — an assertion already verified by an authentication
  adapter. Its authority is the `(issuer, subject)` pair, never an email address
  or a client-supplied user ID.
- **Workspace** — the tenant boundary for channels, collection state, analyses,
  exports, and membership administration.
- **Membership** — the current relationship between one user and one workspace,
  with exactly one role. At most one current membership exists for a
  `(workspace_id, user_id)` pair.
- **Role** — a named bundle of permissions. The MVP roles are `OWNER` and
  `MEMBER`; there are no custom roles.
- **Session** — a revocable server-managed login session for one user. A session
  is not workspace authority and may be used only after current membership is
  checked.
- **Workspace selection** — an untrusted hint identifying the workspace the
  user intends to act in. Selection never grants access.
- **Workspace context** — a short-lived, trusted, immutable result of successful
  session, membership, and permission resolution for exactly one workspace and
  one request.

Deleting a membership does not delete the global user. Deleting a workspace
does not implicitly delete its users. Historical membership/audit records must
not be treated as current authorization.

## Trust boundaries and threat model

```text
Untrusted browser/client
        |
        | cookie + CSRF proof + workspace selector
        v
Web/API adapter ----------------> verified-identity adapter
        |                                  |
        | authenticated session evidence   | verified assertion only
        +----------------+-----------------+
                         v
                 workspace-access
                  |             |
     trusted WorkspaceContext   | access/audit persistence ports
                  v             v
     tenant-scoped consumers   future storage adapters
```

The Web/API adapter, authentication provider, storage implementation, logs,
and every client value are outside the module's trust boundary. A value is not
trusted merely because it was previously returned to a browser.

### Concise STRIDE analysis

| Threat | Required control |
|---|---|
| Spoofing a user or session | Accept identity only from a configured verifier; use an opaque high-entropy session secret; store only its one-way digest; reject expired, revoked, suspended, or unknown sessions |
| Tampering with workspace or role values | Treat selectors, route IDs, headers, cookies, and serialized contexts as untrusted; derive user and role from current server-side records on every request |
| Repudiating sensitive changes | Emit actor, workspace, action, target opaque ID, outcome, timestamp, and correlation ID for session and membership administration; never log secrets or raw identity assertions |
| Information disclosure | Scope resource lookup by workspace in the same repository operation; use non-enumerating errors; minimize PII; redact session and authentication material from logs and exports |
| Denial of service | Bound identifier/input sizes, rate-limit authentication and administrative entry points in the future adapter, and make revocation/idempotency behavior explicit |
| Elevation of privilege | Check a named permission immediately before the protected operation; prevent last-owner removal; apply role/revocation changes on the next request; never trust cached client permissions |

Concurrency is part of the elevation threat: the future persistence adapter
must enforce the last-owner invariant atomically so two concurrent demotions or
removals cannot leave a workspace without an owner.

## Authentication and authorization separation

Authentication answers “which local user controls this verified identity and
session?” Authorization answers “may that user perform this operation in this
workspace now?” They are separate decisions.

- The authentication adapter verifies issuer, subject, signature, audience,
  nonce/state where applicable, authentication time, and provider status before
  producing `VerifiedIdentity`.
- `workspace-access` maps the verified `(issuer, subject)` to one local user and
  establishes or resolves a server-managed session.
- Email address, display name, provider access token, browser storage, URL
  parameter, and workspace selector are never authentication or authorization
  proof.
- Authorization loads a current membership and derives permissions from the
  current role. A session may not embed an authorization snapshot that remains
  valid after a role or membership change.
- Authentication success without a current membership is insufficient for any
  tenant-scoped operation.

Provider linking, step-up authentication, password recovery, and the choice of
identity provider are separate specifications. They may not weaken these
boundaries.

## Typed contracts

The notation below defines stable semantic interfaces, not an implementation or
framework commitment. Public records are immutable; time values are
timezone-aware and normalized to UTC.

```python
@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    authenticated_at: datetime
    verified_email: str | None
    display_name: str | None


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: SessionId
    user_id: UserId
    authenticated_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: WorkspaceId
    user_id: UserId
    membership_id: MembershipId
    role: Role
    permissions: frozenset[Permission]
    session_id: SessionId
    authorization_revision: int
    resolved_at: datetime
```

Only `workspace-access` constructs a trusted `WorkspaceContext`. Consumers do
not deserialize one from HTTP, a queue, a cookie, or client JSON. The context
is valid for one request/operation and is not cached across requests.

### Consumed interfaces

```python
def establish_session(
    identity: VerifiedIdentity,
    request: SessionStartRequest,
) -> IssuedSession:
    """Map verified identity and return a secret once to the cookie adapter."""


def authenticate_session(evidence: SessionEvidence) -> AuthenticatedSession:
    """Validate the opaque secret, expiry, revocation, and user status."""
```

`SessionEvidence` contains the opaque secret received by the ingress adapter.
It is a sensitive input with redacted string/repr behavior. `IssuedSession`
contains the raw secret exactly once; ordinary session reads return only
`session_id` and metadata. No consumer receives a stored digest or provider
credential.

Future adapters supply atomic persistence, a UTC clock, cryptographically
secure random bytes, and audit-event sinks behind interfaces. The specification
does not choose a database, KMS, cache, or Web framework.

### Provided interfaces

```python
def resolve_workspace_context(
    session: AuthenticatedSession,
    selection: WorkspaceSelection | None,
    required_permission: Permission,
) -> WorkspaceContext:
    """Resolve current membership and fail closed unless permission is held."""


def list_accessible_workspaces(
    session: AuthenticatedSession,
) -> tuple[WorkspaceSummary, ...]:
    """Return only workspaces with a current membership, sorted stably."""


def create_workspace(
    session: AuthenticatedSession,
    command: CreateWorkspace,
) -> WorkspaceContext:
    """Create a workspace and its initial OWNER membership atomically."""


def update_workspace(context: WorkspaceContext, command: UpdateWorkspace) -> Workspace:
    ...


def delete_workspace(context: WorkspaceContext, command: DeleteWorkspace) -> None:
    """Authorize deletion; downstream data cascades require their own specs."""


def grant_membership(context: WorkspaceContext, command: GrantMembership) -> Membership:
    ...


def change_membership_role(
    context: WorkspaceContext,
    command: ChangeMembershipRole,
) -> Membership:
    ...


def revoke_membership(context: WorkspaceContext, command: RevokeMembership) -> None:
    ...


def revoke_session(session: AuthenticatedSession, target: SessionId) -> None:
    ...


def revoke_all_user_sessions(session: AuthenticatedSession) -> None:
    ...


def export_my_access_data(session: AuthenticatedSession) -> AccountAccessExport:
    ...


def request_account_deletion(session: AuthenticatedSession) -> None:
    """Revoke sessions or fail atomically when sole-owner obligations remain."""
```

Membership administration targets an existing opaque user ID. Email invitation,
identity discovery, and invitation tokens require a later privacy/security
specification and are not implied by `grant_membership`.

Administrative methods require a context resolved for their named permission
and reject the call if the context lacks it or its authorization revision is no
longer current. A session may revoke only itself or another session belonging to
the same user; there is no cross-user administrator session API in the MVP.

Every consumer operation follows this sequence:

1. Authenticate the server-managed session.
2. Treat the workspace selection as untrusted.
3. Resolve current membership and the named permission into one trusted
   `WorkspaceContext`.
4. Pass that context to the tenant-scoped consumer/repository.
5. Scope the resource lookup by `context.workspace_id` in the same operation.

A resource must never be fetched globally and then checked against the context.
Future background jobs cannot reuse a browser cookie or stale serialized
context; `collection-jobs` must specify separately issued, workspace-bound,
least-privilege execution authority before such jobs are implemented.

## Workspace selection and tenant invariants

- A workspace selector from a path, query, header, form, cookie, or saved
  preference is always a hint and is validated against current membership.
- If an explicit selector is present, it is used only after authorization.
- Without an explicit selector, a still-current server-side preference may be
  tried but must be revalidated. If none exists and exactly one accessible
  workspace remains, that workspace may be selected.
- If multiple workspaces remain and no valid choice exists, return
  `WORKSPACE_SELECTION_REQUIRED`; never pick the first workspace implicitly.
- A user with no current memberships receives `NO_ACCESSIBLE_WORKSPACE` for
  workspace listing/selection and cannot obtain a context.
- A guessed, deleted, or inaccessible workspace returns the same
  `WORKSPACE_NOT_FOUND_OR_FORBIDDEN` result.
- Each context names exactly one workspace. Cross-workspace aggregation is a
  higher-level operation that must authorize every workspace separately.
- The session's user ID is the actor. A client-supplied user ID, membership ID,
  role, permission list, or authorization revision is ignored and rejected
  where present.
- Role or membership changes increment an authorization revision. Regardless
  of revision implementation, the next request must read current authority.
- Tenant-scoped storage reads/writes and uniqueness constraints include
  `workspace_id`; globally unique-looking resource IDs do not remove this rule.

## Roles and permissions

Roles are closed enums in the MVP. Permission names are stable machine values;
adapters own localized labels.

| Permission | OWNER | MEMBER |
|---|:---:|:---:|
| `workspace.read` | Yes | Yes |
| `workspace.update` | Yes | No |
| `workspace.delete` | Yes | No |
| `membership.list` | Yes | Yes |
| `membership.manage` | Yes | No |
| `channel.read` | Yes | Yes |
| `channel.manage_connection` | Yes | No |
| `collection.read` | Yes | Yes |
| `collection.run` | Yes | Yes |
| `analysis.read` | Yes | Yes |
| `analysis.export` | Yes | Yes |
| `audit.read` | Yes | No |

Administrative invariants:

- Workspace creation atomically creates the creator's `OWNER` membership.
- A workspace always has at least one current `OWNER`.
- Only an `OWNER` may grant/revoke membership or change roles.
- The last owner cannot be demoted, removed, or leave. Another owner must first
  be promoted, or an explicit workspace-deletion flow must complete.
- An owner may remove their own non-last-owner membership.
- Repeating a command with the same idempotency key and payload has the same
  effect; reusing the key with different input is a conflict.
- Permission checks occur before protected mutations, and invariant checks plus
  writes are atomic in the future persistence adapter.

## Session lifecycle and browser requirements

The eventual browser adapter uses a server-managed cookie. Bearer credentials
in browser `localStorage` or `sessionStorage`, provider tokens in the browser,
and client-readable authorization snapshots are excluded.

- Session secrets contain at least 128 bits of cryptographically secure
  entropy, are rotated on login and privilege-sensitive reauthentication, and
  are compared using a safe implementation.
- Only a one-way session-secret digest is retained server-side. Raw secrets,
  provider assertions, and provider access/refresh tokens never enter ordinary
  application tables, logs, metrics, traces, exports, or error messages.
- The cookie uses a `__Host-` name with `Secure`, `HttpOnly`, `Path=/`, no
  `Domain`, and at least `SameSite=Lax`. Production traffic is HTTPS only.
- State-changing browser operations require an unpredictable server-validated
  CSRF token bound to the session. SameSite cookies and Origin/Referer checks are
  defense in depth, not the sole CSRF control. Safe methods have no side effects.
- Proposed MVP limits are 30 minutes idle and 12 hours absolute. “Remember me”
  and longer-lived sessions are outside this specification.
- Successful authenticated use may advance idle expiry but never absolute
  expiry. Expiry evaluation uses server UTC time.
- Logout revokes the current session before clearing the cookie. “Log out all”
  revokes all sessions for the user, including the current one.
- Account suspension/deletion and a verified identity-compromise signal revoke
  all user sessions. Membership revocation affects that workspace on the next
  authorization decision while leaving unrelated workspace access intact.
- Role changes affect the next authorization decision. A request that already
  completed authorization may finish atomically, but a queued/retried operation
  must authorize again.
- Revocation and logout are idempotent. Unknown/revoked tokens do not reveal
  whether a user, session, or workspace exists.

## Error semantics

Failures return a typed `WorkspaceAccessError` with stable `code`, a safe
user-facing `message`, optional non-sensitive `field`, `retryable`, and a
correlation ID. Internal causes and identifiers remain in access-controlled
diagnostics only.

Initial codes:

- `UNAUTHENTICATED`
- `SESSION_EXPIRED_OR_REVOKED`
- `CSRF_VALIDATION_FAILED`
- `WORKSPACE_SELECTION_REQUIRED`
- `NO_ACCESSIBLE_WORKSPACE`
- `WORKSPACE_NOT_FOUND_OR_FORBIDDEN`
- `PERMISSION_DENIED`
- `MEMBERSHIP_NOT_FOUND_OR_FORBIDDEN`
- `MEMBERSHIP_ALREADY_EXISTS`
- `LAST_OWNER_REQUIRED`
- `IDEMPOTENCY_CONFLICT`
- `INVALID_INPUT`

Tenant resource consumers use their own stable not-found code, but return it
identically when a resource is absent or belongs to another workspace. Public
messages never include a guessed workspace/resource name, another user's email,
membership state, session secret, provider subject, raw assertion, database
error, or stack trace.

## Privacy, retention, export, and deletion

### Purpose and minimization

Retain only data needed to authenticate an account, display it to collaborators,
authorize workspace access, secure sessions, and audit sensitive changes:

- opaque local user ID and verified `(issuer, subject)` mapping;
- optional verified email and display name for account/collaboration display;
- workspace name and current membership/role;
- session ID, secret digest, creation/use/expiry/revocation times, and minimally
  necessary coarse security metadata;
- access-administration audit events without secrets or raw request payloads.

Do not collect date of birth, address, phone number, private YouTube subscriber
identity, contacts, or provider profile fields unrelated to these purposes.
Do not use email as a durable foreign key or authorization fact.

### Proposed retention schedule

| Data | Retention requirement |
|---|---|
| Active account/workspace/membership | While needed for the active service relationship |
| Raw session secret | Never persisted |
| Active session record/digest | Until revocation or expiry |
| Revoked/expired session record | Purge within 30 days after the latest possible absolute expiry |
| Security/session audit events | 90 days, unless an active abuse investigation requires a documented hold |
| Workspace membership/administration audit events | 1 year, with user PII minimized or pseudonymized after account deletion |
| Deleted-account direct identifiers | Remove or irreversibly anonymize within 30 days, excluding documented legal/security holds |

Retention jobs, legal bases, regional requirements, and backup erasure need a
deployment/privacy review before production. A hold must have an owner, reason,
scope, and expiry; “keep forever” is not a valid default.

### Export and deletion obligations

- A user can export their account profile, verified identity metadata safe for
  disclosure, current workspace memberships/roles, owned workspace summaries,
  and relevant administration history in a machine-readable format.
- Exports exclude session secrets/digests, CSRF tokens, provider credentials,
  internal risk signals, other users' private identifiers, and unrelated
  workspace data.
- Deletion immediately revokes all user sessions and prevents new contexts.
- Deletion is blocked while the user is the sole owner of any workspace. The
  user must transfer ownership or explicitly delete each affected workspace.
- Once ownership is resolved, direct user identifiers are deleted/anonymized
  within the schedule above; retained audit events use an opaque tombstone.
- Workspace deletion authorization belongs here, while cascading deletion of
  channels, snapshots, jobs, analyses, and exports is specified by their owning
  modules before persistence is implemented.

## Tech stack

- Python 3.12 for domain contracts and tests, consistent with `analytics-core`.
- Standard-library `dataclasses`, `datetime`, `enum`, `hashlib`/`hmac`, and
  `secrets` are sufficient for contract-level implementation.
- Existing `unittest` runner; no new dependency in this module slice.
- Authentication provider, Web framework, database, cache, queue, encryption
  service, and deployment platform remain intentionally undecided.

Choosing password hashing, session middleware, an identity SDK, or distributed
context signing requires a separate source-grounded security review; standard
library primitives must not be assembled into a custom password or token
protocol.

## Proposed project structure

No source directories are created while this specification is under review.
After approval, the implementation plan may propose:

```text
workspace_access/
  models.py              # Immutable IDs, records, roles, permissions, errors
  service.py             # Session/context/membership orchestration
  ports.py               # Clock, identity mapping, persistence, audit ports
  tests/
    test_sessions.py
    test_authorization.py
    test_memberships.py
    test_privacy.py
```

Framework/database adapters remain outside this package and outside the first
implementation slice.

## Code style

Use explicit permission requests, immutable contexts, typed errors, UTC times,
and one tenant boundary per operation:

```python
def get_channel_summary(
    access: WorkspaceAccess,
    repository: ChannelSummaryRepository,
    session: AuthenticatedSession,
    selection: WorkspaceSelection,
    channel_id: ChannelId,
) -> ChannelSummary:
    context = access.resolve_workspace_context(
        session,
        selection,
        required_permission=Permission.CHANNEL_READ,
    )
    return repository.get_for_workspace(context, channel_id)
```

Do not accept a bare `workspace_id`, user/role from request JSON, global mutable
“current tenant,” framework request object, provider token, or raw cookie in a
tenant repository. Keep provider/framework translation at adapters.

## Testing strategy

Implementation is fixture-only and test-first. It uses an in-memory fake behind
the same atomic semantic ports expected of future persistence.

- Table-driven role/permission tests for every matrix cell.
- Context-resolution tests for explicit selection, one-workspace fallback,
  multiple-workspace ambiguity, stale preference, zero memberships, and guessed
  or cross-tenant identifiers.
- Session tests at exact idle/absolute cutoffs, rotation, logout, logout-all,
  revocation, suspension, and redacted repr/log behavior.
- Membership tests for grant, duplicate grant, role change, revocation,
  self-removal, last-owner protection, atomic concurrent last-owner attempts,
  idempotent retry, and next-request effect.
- Cross-tenant negative tests proving identical external results for absent and
  foreign workspace/resources and proving no foreign data reaches a result.
- CSRF contract tests for every state-changing browser command; framework-level
  cookie/header tests wait for the Web adapter specification.
- Property/invariant tests using deterministic generated fixtures without a new
  property-testing dependency: a workspace never reaches zero owners and a
  resolved context always has one current membership.
- Privacy tests for export allowlists, deletion ownership blocks, audit
  redaction, session-secret one-time return, and retention cutoffs.
- Mutation/determinism tests for public immutable records and stable workspace
  listing order.

Proposed commands after an approved implementation plan:

```powershell
# Focused workspace-access suite
python -m unittest discover -s workspace_access/tests -v

# Existing analytics regression suite
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q workspace_access subscriber_analytics

# Whitespace and patch integrity
git diff --check
```

There is currently no configured formatter, linter, type checker, or CI
workflow. Adding one is outside this specification and requires review.

## Boundaries

### Always

- Authenticate first, then resolve current membership and a named permission.
- Treat every tenant selector and resource ID from a client as untrusted.
- Pass one trusted immutable `WorkspaceContext` to every tenant-scoped
  operation and scope storage access by the same workspace.
- Recheck current authority on every request and after every queued/retried
  boundary.
- Keep at least one owner and enforce that invariant atomically.
- Use secure server-managed sessions, explicit CSRF protection, UTC time,
  stable safe errors, minimal PII, and secret-free audit events.
- Test both permitted behavior and cross-workspace denial before persistence.

### Ask first

- Add or upgrade any dependency.
- Choose an authentication provider, Web framework, database, cache, KMS,
  queue, deployment platform, session duration, or cookie/CSRF library.
- Add roles/permissions or change the permission matrix.
- Change retention periods, deletion/export semantics, or owner safeguards.
- Introduce HTTP routes, invitation emails/tokens, service accounts, API keys,
  bearer tokens, or background-job authority.

### Never

- Trust a client-supplied workspace, user, membership, role, permission, or
  serialized context as authority.
- Fetch a tenant resource globally and authorize it afterward.
- Reveal whether a guessed workspace/resource exists outside the current
  context.
- Store browser bearer tokens in Web Storage or persist raw session secrets.
- Put session/OAuth secrets, provider assertions, raw payloads, or sensitive PII
  in source control, logs, metrics, traces, errors, exports, or ordinary tables.
- Implement password authentication, Google/YouTube OAuth, token persistence,
  real-user collection, or production deployment under this specification.

## Explicit non-goals

- Database schemas, migrations, ORM entities, or persistence implementation.
- HTTP endpoints, middleware, browser pages, or frontend state.
- Password registration/login/recovery or authentication-provider selection.
- Google/YouTube OAuth, channel connection, refresh-token encryption, or KMS.
- Invitations, domain-based automatic membership, SSO, SCIM, custom roles, or
  billing organizations.
- Service accounts, public API tokens, background-job credentials, or
  cross-workspace administrator access.
- Channel data, collection jobs, analyses, exports, or their deletion cascades.
- Real credentials, real account data, or production deployment.

## Success criteria

1. Authentication, session resolution, workspace selection, membership lookup,
   and permission authorization are separate, explicit decisions.
2. Every tenant-scoped consumer contract requires a trusted single-workspace
   context; no client value can construct or extend that authority.
3. The `OWNER`/`MEMBER` matrix covers all currently mapped downstream actions
   and last-owner protection is atomic and testable.
4. Explicit, implicit, stale, ambiguous, absent, and foreign workspace
   selections have deterministic fail-closed behavior.
5. Role changes, membership revocation, session revocation, expiry, account
   suspension, and deletion have explicit next-request invalidation semantics.
6. Browser requirements mandate secure server-managed cookies and independent
   CSRF protection, with no browser-stored bearer/provider tokens.
7. Cross-workspace lookups and errors do not disclose resource existence, and
   negative isolation tests cover every future repository adapter.
8. Stable typed errors and audit events contain no secret, raw assertion,
   foreign identifier, stack trace, or unnecessary PII.
9. Purpose limitation, data minimization, retention, account export, ownership
   transfer/deletion blocks, and anonymization obligations are testable.
10. The first implementation can use only fixtures/in-memory fakes and the
    standard library; it adds no provider, database, Web, OAuth, or production
    credential behavior.
11. Focused access tests, existing analytics regressions, compile validation,
    secret-value review, and `git diff --check` pass before implementation is
    considered complete.
12. `channel-data` and all later specs can cite this module without inventing a
    second tenant or permission interpretation.

## Review decisions requested

Please approve or revise these intentional choices before planning:

1. The MVP has only `OWNER` and `MEMBER`; channel connections and membership
   changes are owner-only, while members may run collections and analyses.
2. Missing/foreign workspaces and resources use non-enumerating errors rather
   than exposing whether the identifier exists.
3. Browser sessions use secure server-managed cookies with proposed 30-minute
   idle and 12-hour absolute limits; longer-lived “remember me” is deferred.
4. Role/membership changes affect the next request; membership removal does not
   log the user out of unrelated workspaces.
5. The proposed retention defaults are 90 days for security/session events and
   one year for membership/administration audit events, with deletion-time PII
   minimization.
6. Invitations and background-job authority remain separate reviewed contracts
   rather than being implicitly included in membership/session APIs.
