# Channel Connections Specification

Status: Approved
Module id: `channel-connections`
Date: 2026-08-20
Approved: 2026-08-20 (all seven review decisions)

## Objective

Define the tenant-scoped contract that lets a workspace owner connect,
reauthorize, inspect, and disconnect one or more YouTube channels without
exposing OAuth credentials to browsers, application records, logs, other
workspaces, or analytics consumers.

This module serves workspace owners, workspace members who need connection
status, future HTTPS/OAuth adapters, collection executors, privacy cascades,
and `channel-data`. It establishes the security and lifecycle invariants before
choosing a Web framework, database, credential vault, KMS, queue, or Google SDK
version.

### User-visible behavior

- An owner can start a hosted YouTube authorization flow for the current
  workspace and is redirected only to the configured Google authorization
  endpoint.
- The callback succeeds only for the same current user session, workspace,
  one-time authorization intent, exact redirect URI, and PKCE transaction.
- A connection becomes visible only after the provider grant is exchanged,
  its scope is verified, exactly one API-visible channel is identified, the
  subscriber capability probe succeeds, and credentials are placed in the
  credential vault.
- Members may see safe channel connection metadata and readiness, but only
  owners may start, complete, reauthorize, or disconnect a connection.
- Revoked or invalid provider credentials fail closed as `REAUTH_REQUIRED` and
  cannot be used by future collection execution.
- Disconnecting immediately prevents credential use and removes the credential
  vault slot. Previously collected channel data remains until an explicit
  `channel-data` deletion operation or workspace cascade removes it.
- The same YouTube channel may be connected independently in two workspaces;
  connection state, credentials, authorization intents, idempotency, errors,
  and deletion never cross that boundary.

### Future Web/UI acceptance requirement

The final hosted product must be understandable and operable by a
non-engineer. UI implementation remains outside this approved reference slice,
but the later Web/UI specification and acceptance tests must require:

- a guided connection flow that presents one clear primary action at each step;
- plain Japanese labels and explanations, with OAuth or security terminology
  avoided unless it is immediately explained in user language;
- connection status, data readiness, limitations, and the next recommended
  action shown together rather than as raw machine states;
- safe, actionable error messages that explain what the user can do next
  without exposing provider responses, credentials, or internal identifiers;
- accessible keyboard operation, visible focus, semantic controls, readable
  contrast, and assistive-technology labels; and
- responsive behavior suitable for ordinary desktop and mobile use.

Browser-based usability and accessibility verification is required when that
future UI slice is implemented. These requirements do not authorize Web routes,
real OAuth, credentials, provider calls, or UI work in the current slice.

## Source-grounded provider constraints

The future provider adapter must follow these current authoritative sources:

- Google Web Server OAuth uses the authorization-code flow, registered redirect
  URIs, offline access for refresh tokens, and a `state` parameter to reduce
  CSRF risk:
  https://developers.google.com/identity/protocols/oauth2/web-server
- Google's YouTube server-side OAuth guide requires HTTPS, correctly configured
  redirect URIs, `state` validation, secure token storage, and supports the
  read-only YouTube scope:
  https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps
- Google OAuth policy requires HTTPS origins/redirects, least-privilege scopes,
  secure credential handling, revocation handling, and token revocation when
  access is no longer needed:
  https://developers.google.com/identity/protocols/oauth2/policies
- `channels.list(mine=true)` returns channels owned by the authenticated user
  and is valid only on a properly authorized request:
  https://developers.google.com/youtube/v3/docs/channels/list
- `subscriptions.list(mySubscribers=true)` retrieves the authenticated user's
  subscribers; the provider may return only a limited observable subset:
  https://developers.google.com/youtube/v3/docs/subscriptions/list
- OAuth 2.0 Security Best Current Practice recommends PKCE for confidential
  clients, requires transaction-specific binding, identifies S256 as the safe
  challenge method, requires CSRF protection, and rejects open redirects:
  https://www.rfc-editor.org/rfc/rfc9700.html#section-2.1

The only requested provider scope is:

```text
https://www.googleapis.com/auth/youtube.readonly
```

No upload, delete, write, Analytics API, Partner API, identity-login, email,
profile, or broad Google scope is permitted by this specification.

The existing `subscriber_analytics` `InstalledAppFlow` and plaintext local
`token.json` cache remain local CLI behavior. Hosted code must not read, copy,
import, or migrate those files or environment JSON values into a connection.

## Domain language and data classification

- **Connection intent** — a short-lived, one-time server record that binds one
  OAuth transaction to workspace, user, browser session, redirect URI, provider,
  operation, and PKCE secret handle.
- **Provider grant** — a transient verified result from the OAuth/provider
  gateway. It includes exact scopes, one selected provider channel, capability
  proof, token expiry, and redacted credential values. It is never a public
  application record.
- **Channel connection** — safe workspace-owned metadata that states whether a
  provider channel is ready or requires reauthorization. It contains no token,
  client secret, authorization code, PKCE verifier, state value, vault handle,
  provider response, or credential fingerprint.
- **Credential slot** — an opaque identifier understood only by the credential
  vault. The in-memory domain state may retain the slot identifier but never the
  credential material.
- **Connection audit event** — secret-free evidence of an administrative action,
  containing only opaque IDs, action, outcome, time, and correlation ID.

Data classes:

| Class | Examples | Handling |
|---|---|---|
| Sensitive secret | access/refresh token, authorization code, PKCE verifier, client secret, raw state | transient redacted wrapper or approved secret store only; never logged/exported |
| Personal identifier | provider channel ID, channel title, local actor ID | tenant-scope, minimize, include in applicable workspace export/delete |
| Non-personal operational | status, timestamps, provider enum, safe reason code | normal tenant-scoped handling |
| Prohibited | raw provider payload, email/profile identity, subscriber rows, token fingerprint | never retain in this module |

## Permissions and authority

Every browser/API adapter authenticates the server-managed session and resolves
a fresh `WorkspaceContext` before calling this module.

| Operation | Required permission |
|---|---|
| List/get safe connection metadata | `channel.read` |
| Start/complete a new connection | `channel.manage_connection` |
| Start/complete reauthorization | `channel.manage_connection` |
| Disconnect a connection | `channel.manage_connection` |
| Workspace-scoped retention purge | `channel.manage_connection` |
| Workspace connection cascade | `workspace.delete` |

The module checks the exact named permission itself. It does not infer authority
from `Role`, channel ownership claims, provider tokens, local user IDs, route
parameters, or serialized permissions.

Callback completion requires a newly resolved context whose `workspace_id`,
`user_id`, and `session_id` equal the intent bindings. A different owner cannot
complete another owner's in-flight callback even within the same workspace.
Role or membership changes therefore take effect on the next callback request.

Future background collection cannot reuse a browser context or request a raw
credential. It needs a separately specified current, workspace-bound execution
authority and a brokered provider-operation interface. That interface is not
authorized by this specification.

## Connection model

Stable enums:

```python
class ConnectionProvider(str, Enum):
    YOUTUBE = "YOUTUBE"


class ConnectionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
```

`ChannelConnection` public fields:

- `connection_id: str`
- `workspace_id: str`
- `provider: ConnectionProvider`
- `provider_channel_id: str`
- `channel_title: str`
- `status: ConnectionStatus`
- `connected_at: datetime`
- `updated_at: datetime`

All public values are immutable/slotted. IDs and titles are bounded opaque text;
datetimes are timezone-aware and normalized to UTC. No public representation or
error reveals credential presence, token expiry, vault identifiers, actor IDs,
provider account identity, or raw provider failure details.

The active uniqueness key is
`(workspace_id, provider, provider_channel_id)`. The same provider channel ID
may exist in another workspace with an unrelated connection and credential
slot. A disconnected connection is removed from the active key and may later be
connected using a new connection ID.

## Authorization intent and callback

`begin_authorization` creates one intent with:

- a server-generated high-entropy intent ID;
- workspace, user, and current session bindings;
- operation `CONNECT` or `REAUTHORIZE`;
- optional target connection ID for reauthorization;
- exact provider and preconfigured HTTPS redirect URI identifier;
- a one-time high-entropy state value whose digest, not raw value, is indexed;
- a PKCE S256 verifier held in an ephemeral secret-store slot and its challenge;
- requested scope fixed to `youtube.readonly`;
- creation and expiry at exactly 10 minutes;
- actor/payload-bound idempotency metadata.

`AuthorizationStart` returns only:

- `intent_id`;
- the provider authorization URL produced by the allowlisted gateway;
- `expires_at`.

The URL must use HTTPS and the configured Google authorization host. The
application does not accept a provider host, redirect URI, post-callback URL,
or scope from a client. Return navigation is a server-side allowlisted route,
not a free-form URL carried in state.

The callback adapter accepts only bounded `code`, `state`, provider-safe error
code, and its authenticated server session. It never accepts a workspace ID,
user ID, connection ID, redirect URI, scope, channel ID, or vault slot from the
browser.

Completion proceeds under these gates:

1. Hash the raw state and find an unexpired intent without revealing whether a
   missing value ever existed.
2. Resolve a fresh `channel.manage_connection` context and compare workspace,
   user, and session bindings in constant-time where secret values are involved.
3. Atomically claim the intent before any authorization-code exchange. An
   in-flight duplicate receives a stable retryable conflict; it never performs
   a second exchange.
4. Load and consume the PKCE verifier from the ephemeral secret store.
5. Exchange the code through the fixed Google gateway using the configured
   client, exact redirect URI, and PKCE verifier.
6. Require a refresh token for offline hosted collection and reject any grant
   whose granted scopes are not exactly the approved scope set.
7. Call `channels.list(mine=true)` and require exactly one API-visible channel;
   never select the first of an ambiguous result.
8. Call `subscriptions.list(mySubscribers=true, maxResults=1)` and require a
   successful capability result. The count is not stored.
9. Atomically reserve the tenant-scoped active uniqueness key and deterministic
   credential slot without making a connection visible. If another active or
   in-flight connection owns the key, revoke the new grant and fail safely.
10. Put the credential bundle into that slot, then atomically publish safe
    connection metadata that references the slot internally. A failed put
    releases the invisible reservation and leaves explicit cleanup state.
11. Remove ephemeral secrets and retain only a secret-free consumed-intent
    result for bounded exact replay.

Provider denial or error consumes the intent and removes ephemeral secrets. A
new user action starts a new intent. Safe error codes never contain provider
descriptions, channel candidates, tokens, authorization codes, or foreign IDs.

An exact duplicate callback from the same bound session and payload returns the
original safe result without exchanging the code again. A changed actor,
session, state/code digest, operation, or payload fails as a callback conflict.
Consumed callback replay metadata expires after 24 hours.

## Reauthorization

Reauthorization is a distinct operation naming one existing connection. It
uses the same intent, state, PKCE, scope, ownership, and capability gates.

- The verified provider channel ID must exactly match the target connection.
- A mismatch fails without replacing or deleting the current credential.
- A successful vault write rotates the credential slot and updates the
  connection atomically from the reader's perspective.
- The old slot is deleted immediately after the new connection revision is
  committed; failed cleanup is tracked by a secret-free cleanup identifier for
  retry, never by copied token material.
- `REAUTH_REQUIRED` returns to `ACTIVE` only after the full verification succeeds.

## Credential custody

The domain implementation depends on two ports:

```python
class EphemeralSecretStore(Protocol):
    def put(
        self,
        workspace_id: str,
        slot_id: str,
        secret: RedactedSecret,
        expires_at: datetime,
    ) -> None: ...
    def take(self, workspace_id: str, slot_id: str) -> RedactedSecret: ...
    def delete(self, workspace_id: str, slot_id: str) -> None: ...


class CredentialVault(Protocol):
    def put(
        self,
        workspace_id: str,
        slot_id: str,
        credential: ProviderCredential,
    ) -> None: ...
    def delete(self, workspace_id: str, slot_id: str) -> None: ...
```

`RedactedSecret`, `ProviderCredential`, and vault APIs are private/internal and
excluded from the package root. Their `str`/`repr` are always redacted. Public
services have no `get_token`, `reveal_credential`, or export method.

The reference fake may hold deterministic synthetic secrets only in memory to
prove the contract. It is not encryption and must be named/documented as a fake.
Production requires an approved managed KMS/secret store or envelope-encrypted
credential repository, key rotation, access auditing, backup erasure, and a
deployment-specific recovery procedure. No standard-library custom encryption
scheme is permitted.

Credential slots are deterministic from internal opaque intent/connection IDs,
so retries cannot create unbounded orphan copies. A failed callback deletes its
slot. A startup/retention reconciler must remove expired ephemeral slots and
unreferenced credential slots without reading secrets into application logs.

## Provider gateway boundary

```python
class YouTubeAuthorizationGateway(Protocol):
    def authorization_url(
        self,
        *,
        state: RedactedSecret,
        code_challenge: str,
        redirect_uri_id: str,
        scopes: tuple[str, ...],
    ) -> str: ...

    def exchange_and_verify(
        self,
        *,
        code: RedactedSecret,
        code_verifier: RedactedSecret,
        redirect_uri_id: str,
    ) -> VerifiedProviderGrant: ...

    def revoke(
        self,
        workspace_id: str,
        credential_slot_id: str,
    ) -> RevocationOutcome: ...
```

The production gateway owns fixed provider endpoints, HTTP timeouts, response
size limits, TLS validation, schema validation, Google SDK/version choice,
provider error mapping, capability calls, and revocation. External responses
are untrusted until converted into a strictly validated `VerifiedProviderGrant`.

The domain service never accepts an arbitrary URL and performs no server fetch
to a caller-influenced host, preventing an OAuth configuration surface from
becoming SSRF.

## Public domain interfaces

```python
class ConnectionReader(Protocol):
    def get_connection(
        self,
        context: WorkspaceContext,
        connection_id: str,
    ) -> ChannelConnection: ...

    def list_connections(
        self,
        context: WorkspaceContext,
        page: ConnectionPageRequest = ConnectionPageRequest(),
    ) -> ConnectionPage: ...


class ConnectionManager(Protocol):
    def begin_authorization(
        self,
        context: WorkspaceContext,
        command: BeginAuthorization,
    ) -> AuthorizationStart: ...

    def complete_authorization(
        self,
        context: WorkspaceContext,
        command: CompleteAuthorization,
    ) -> ChannelConnection: ...

    def begin_reauthorization(
        self,
        context: WorkspaceContext,
        command: BeginReauthorization,
    ) -> AuthorizationStart: ...

    def disconnect(
        self,
        context: WorkspaceContext,
        command: DisconnectConnection,
    ) -> None: ...


class ConnectionPrivacyAdministrator(Protocol):
    def delete_workspace_connections(
        self,
        context: WorkspaceContext,
        command: DeleteWorkspaceConnections,
    ) -> None: ...

    def purge_retention(
        self,
        context: WorkspaceContext,
        reference_time: datetime,
    ) -> ConnectionRetentionReport: ...
```

List pagination uses an opaque server-side cursor bound to workspace, ordering,
limit, and a captured immutable result. Limits are 1-100, default 50; ordering
is `connected_at` descending then `connection_id` ascending. Invalid,
cross-workspace, cross-query, tampered, reused, or expired cursors share one
safe error. No cursor contains provider/channel metadata or credentials.

Future HTTP mapping is additive and outside this slice. It is expected to use
plural resources (`/api/channel-connections`) and one structured safe error
shape; no callback value is returned as HTML without encoding.

## Stable safe errors

Initial machine codes:

- `INVALID_INPUT`
- `PERMISSION_DENIED`
- `CONNECTION_NOT_FOUND_OR_FORBIDDEN`
- `CONNECTION_ALREADY_EXISTS`
- `INTENT_NOT_FOUND_OR_EXPIRED`
- `CALLBACK_CONFLICT`
- `OPERATION_IN_PROGRESS`
- `PROVIDER_AUTHORIZATION_FAILED`
- `PROVIDER_CAPABILITY_MISSING`
- `REAUTH_CHANNEL_MISMATCH`
- `INVALID_CURSOR`
- `CURSOR_EXPIRED`

Errors expose only code, stable generic message, optional safe field, retryable
flag, opaque correlation ID, and optional closed reason code. Missing and
foreign resources are indistinguishable. Provider HTTP status, response body,
email/profile data, candidate channel list, title, token timing, scopes beyond
the approved constant, vault errors, and stack traces are never public.

## State, idempotency, concurrency, and failure outcomes

All state-changing commands contain a bounded idempotency key. The reference
service serializes each state transition with one lock. Future persistence must
claim uniqueness and mutate state transactionally.

- Idempotency is keyed by workspace, actor, operation, and key, and is bound to
  a canonical payload fingerprint.
- Exact begin replay returns the original unexpired authorization URL/result;
  changed reuse conflicts.
- Concurrent begin attempts may coexist because each represents an explicit
  intent, but one idempotency key creates at most one intent.
- Callback intent claim is atomic. One exchange wins; duplicates never call the
  provider a second time.
- Publishing a connection and changing the active uniqueness key are atomic.
- A timeout from provider exchange, vault write, or revocation is an unknown
  external outcome, not proof of failure. The intent/cleanup record remains
  explicit and future retries reconcile by deterministic identifiers rather
  than repeating a side effect blindly.
- Mutation idempotency records persist 90 days. Unresolved operations are not
  silently purged.

## Disconnect, revocation, and deletion

Disconnect follows this fail-closed order:

1. Atomically mark the connection unavailable for new credential leases.
2. Ask the gateway to revoke provider access using the internal credential slot.
3. Delete the credential slot regardless of whether provider revocation can be
   confirmed.
4. Remove public connection metadata and the active uniqueness key.
5. Retain only secret-free idempotency/audit/cleanup evidence.

An unconfirmed provider revocation is recorded as a secret-free cleanup outcome;
the token is not retained merely to retry. The user may also revoke the app in
their Google account. Detection of revoked/expired refresh authority deletes the
slot and publishes `REAUTH_REQUIRED` without exposing provider details.

Disconnect does not delete `channel-data`. An owner uses its separately
authorized deletion operation when collected observations must also be erased.
Workspace deletion must coordinate both modules: connection credentials are
removed immediately and channel data follows its approved cascade.

The connection is owned by the workspace after an owner authorizes it. Removing
the authorizing local user does not automatically disconnect a shared workspace
connection, and this module does not retain an `authorized_by_user_id` field.
The administrative audit event records the actor under the workspace-access
retention policy. Workspace owners remain able to disconnect at any time.

## Retention, privacy, and audit

| Record | Retention |
|---|---|
| Unclaimed authorization intent and PKCE slot | Delete at exactly 10 minutes |
| Consumed callback replay record | 24 hours |
| Active/reauth-required connection metadata | Until disconnect or workspace deletion |
| Credential slot | Until credential invalidation, disconnect, or workspace deletion |
| Mutation idempotency and unresolved cleanup metadata | 90 days after terminal resolution; unresolved records require explicit reconciliation |
| Connection administration audit events | 1 year under the workspace-access audit policy |
| Disconnected/deleted connection metadata | Remove immediately except opaque IDs in scheduled audit/idempotency tombstones |

Audit actions are `AUTHORIZATION_STARTED`, `CONNECTION_ESTABLISHED`,
`REAUTHORIZATION_STARTED`, `CONNECTION_REAUTHORIZED`,
`CONNECTION_REAUTH_REQUIRED`, `CONNECTION_DISCONNECTED`, and
`WORKSPACE_CONNECTIONS_DELETED`. Events contain actor, workspace, action,
opaque target/intent ID, outcome, timestamp, and correlation ID only. They
exclude provider channel title/ID where an opaque connection ID is sufficient.

Workspace export may include safe connection metadata but never credential
slots, state/code digests, scopes beyond the fixed product constant, provider
errors, or audit-internal cleanup state. Workspace deletion removes active
metadata, intents, cursors, idempotency records not needed for exact deletion
replay, ephemeral secrets, and credential slots without affecting another
workspace using the same provider channel ID.

Production backup erasure, legal holds, regional consent text, KMS audit, and
Google OAuth verification are deployment/privacy review requirements. A hold
must have an owner, purpose, scope, and expiry.

## Threat model

| Threat | Required control |
|---|---|
| Spoofing a callback/user | Authenticate server session; fresh workspace permission; bind state to workspace/user/session; one-time state; PKCE S256 |
| Tampering with workspace, redirect, scope, channel, or connection | Never accept them from callback; use server configuration and verified provider response; tenant-scope every key |
| Repudiation of connection changes | Secret-free actor/workspace/action/outcome audit events and opaque correlation IDs |
| Credential/channel disclosure | Vault/ephemeral secret ports, redacted wrappers, safe records/errors, no raw payloads, no token-return API |
| CSRF/login swapping | One-time state bound to session plus PKCE; exact redirect URI; no open redirect; fresh owner context |
| Authorization-code injection/replay | PKCE S256, atomic intent claim, exact callback replay record, code digest only |
| Cross-tenant guessed IDs | Workspace in the same lookup/uniqueness operation; identical non-enumerating errors |
| Excess provider scope | Fixed exact `youtube.readonly` request and verified grant scope set; revoke unexpected grants |
| Wrong YouTube channel | Exactly one `channels.list(mine=true)` result; reauth must match existing provider channel ID |
| False silent-analysis readiness | Successful `mySubscribers` capability probe; owner-video comment coverage remains a separate `channel-data` gate |
| SSRF/open redirect | Fixed provider hosts and redirect IDs; no caller URL; exact HTTPS allowlists; no arbitrary return URL |
| Retry storm/unknown external effect | Bounded input/rate limit in adapter; atomic claims; deterministic slots; explicit in-progress/cleanup state |
| Stolen or revoked refresh token | Encrypted vault, least privilege, access audit, deletion on invalidation, `REAUTH_REQUIRED`, provider revocation on disconnect |
| Workspace deletion leaves secrets | Workspace connection cascade deletes all vault and ephemeral slots without affecting foreign workspaces |

HTTP adapters must add strict security headers, secure server-managed cookies,
CSRF protection for connection-start/disconnect commands, callback rate limits,
request/body size limits, and safe output encoding. Those framework controls are
not implemented by this standard-library slice.

## Tech stack

- Python standard library for immutable contracts, hashing, high-entropy token
  generation, UTC time, canonical fingerprints, locking, and in-memory fakes.
- Existing `workspace_access.models.WorkspaceContext` and `Permission` are the
  only tenant/authorization interpretation.
- Provider SDK, HTTP client, Web framework, database, queue, cache, KMS, and
  encryption implementation remain undecided.
- No dependency is added or upgraded by the reference slice.

## Commands

Run from the repository root:

```powershell
python -m unittest discover -s channel_connections/tests -v
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q channel_connections workspace_access channel_data subscriber_analytics
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"
git diff --check
```

There is currently no configured formatter, linter, type checker, CI workflow,
or authoritative locked installation boundary for this standard-library module.
The existing unpinned Google dependencies are not executed by its tests.

## Proposed project structure

```text
channel_connections/
  __init__.py            # Small public protocol/service/error surface
  errors.py              # Stable safe error contract
  models.py              # Immutable public values and commands
  ports.py               # Reader/manager, clock/token, gateway, vault, audit ports
  memory.py              # Private in-memory state and deterministic fake secret stores
  service.py             # Permission, intent, callback, lifecycle orchestration
  README.md              # Safe usage, lifecycle, limitations, verification
  tests/
    test_models.py
    test_authorization.py
    test_connections.py
    test_tenant_isolation.py
    test_privacy.py
```

Provider HTTP/SDK adapters, Web routes, persistence, jobs, migrations, and KMS
belong to later implementation slices and are not placed in this package now.

## Code style

Use immutable commands, a trusted context on every tenant method, a redacted
secret boundary, and server-selected provider configuration:

```python
@dataclass(frozen=True, slots=True)
class BeginAuthorization:
    idempotency_key: str


def begin_authorization(
    context: WorkspaceContext,
    command: BeginAuthorization,
) -> AuthorizationStart:
    require(context, Permission.CHANNEL_MANAGE_CONNECTION)
    # workspace/user/session bindings come from context; URLs and scopes come
    # from trusted configuration, never from the command.
    ...
```

Input and output models are separate. Helpers, provider grants, secret wrappers,
vault handles, fingerprints, and internal cleanup state remain private unless a
second trusted module needs an approved contract.

## Testing strategy

Implementation follows red-green-refactor using real in-memory state, fixed
clock/token sources, a strict fake YouTube gateway, and fake secret stores. No
test contacts Google, a network, browser, database, KMS, or real account.

- Contract tests cover immutable/slotted values, stable enums/errors, UTC,
  bounded identifiers/titles/callback values, cursor limits, and redacted secret
  rendering.
- Authorization tests cover state/session/workspace/user binding, 10-minute
  expiry, PKCE S256 inputs, exact redirect/scope constants, denial/error paths,
  one-time claim, callback replay, in-flight duplicate behavior, and ephemeral
  secret deletion.
- Provider verification tests cover zero/multiple channels, unexpected scopes,
  absent refresh token, failed `mySubscribers`, malformed provider values, and
  raw-provider-error suppression.
- Connection tests cover atomic visibility, duplicate provider channel,
  reauthorization exact-channel match, credential rotation, revoked credential
  transition, disconnect ordering, provider-revocation uncertainty, and
  collected-data non-deletion.
- Tenant tests use identical connection/channel/intent/idempotency IDs in two
  workspaces and prove no read, callback, cursor, secret slot, reauth, or cascade
  crosses the boundary.
- Privacy tests inspect every state/store/error/audit/export representation,
  exact 10-minute/24-hour/90-day/1-year boundaries, orphan cleanup, and workspace
  deletion while preserving identical foreign records.
- Concurrency tests prove one callback exchange/publish winner and no credential
  overwrite during racing new connection, reauthorization, and disconnect.
- Integration tests resolve real workspace contexts, publish a ready connection
  reference for `channel-data`, and preserve CLI/Notebook behavior without
  importing local `token.json` or environment OAuth JSON.

Final review uses the code-review graph first, then five-axis, security, and
simplification checks. Static graph attribution is not treated as test execution.

## Boundaries

### Always

- Authenticate and resolve a fresh exact permission before every operation.
- Bind intent, callback, cursor, idempotency, connection, and secret-slot keys to
  the same workspace and operation.
- Use authorization code flow, one-time state, PKCE S256, exact HTTPS redirect,
  fixed provider hosts, and the single read-only scope.
- Treat provider responses as untrusted; validate before state or vault writes.
- Keep credentials in approved secret-store ports and all public/log/error/audit
  representations secret-free.
- Fail closed on ambiguous channel identity, missing offline credential,
  capability failure, revoked grant, unknown external outcome, or cleanup gap.
- Delete credentials on disconnect/workspace deletion and test exact privacy
  cascades before production persistence.

### Ask first

- Approve this specification and its seven review decisions before planning.
- Add or upgrade any dependency or choose Google SDK versions.
- Choose a Web framework, HTTP routes, cookie/CSRF middleware, database, cache,
  queue, KMS/vault, encryption/key rotation, deployment platform, or provider
  client configuration.
- Store or process any real client secret, authorization code, access token,
  refresh token, provider account, channel, subscriber, or comment data.
- Change OAuth scopes, redirect hosts, session policy, permission matrix,
  connection ownership, disconnect/data-deletion behavior, or retention.

### Never

- Use implicit/device/password flows or the desktop loopback flow for hosted use.
- Accept workspace/user/role/permission/scope/redirect/provider URL/channel ID/
  connection target from callback as authority.
- Put credentials, raw provider payloads/errors, code/state/PKCE values, token
  digests, or vault identifiers in source, chat, logs, metrics, traces, errors,
  audit, exports, analytics, ordinary tables, browser storage, or URLs beyond
  the protocol-required short-lived authorization request/callback parameters.
- Implement custom standard-library encryption or claim that an in-memory fake
  is secure storage.
- Fetch a connection globally and authorize after retrieval, reuse credentials
  across workspaces, silently select the first provider channel, or keep using
  a revoked/ambiguous grant.
- Perform real OAuth/provider calls or import the local CLI `token.json` in this
  reference implementation.

## Explicit non-goals

- Production Google OAuth, consent-screen verification, client registration,
  provider HTTP/SDK implementation, real quota use, or real channel connection.
- Database schemas, migrations, ORM, distributed transactions, queue/jobs,
  credential lease/execution authority, or collection scheduling.
- Web endpoints, callback pages, cookies, CSRF middleware, rate limiting,
  security headers, CORS, frontend/UI, or localization.
- Managed KMS/vault selection, encryption implementation, key rotation, backup
  recovery, legal holds, or deployment secrets.
- Adding analytics behavior, changing CLI/Notebook OAuth, collecting data, or
  changing `channel-data` retention/coverage rules.
- Multiple providers, write/upload scopes, service accounts, API keys as a
  substitute for owner OAuth, delegated Studio Editor access, or account linking.

## Success criteria

1. Every tenant method requires a trusted one-workspace context and exact
   `channel.read`, `channel.manage_connection`, or `workspace.delete` permission.
2. One-time state and PKCE S256 are transaction-specific, bound to current
   workspace/user/session, expire at 10 minutes, and cannot cause two exchanges.
3. Scope, redirect, provider endpoints, and return routes are fixed server-side;
   no caller-influenced URL, broader scope, or open redirect is possible.
4. A connection is published only after an offline exact-scope grant, exactly
   one `mine=true` channel, successful `mySubscribers=true` probe, and vault put.
5. Active connection uniqueness, callback replay, reauthorization matching,
   credential rotation, concurrency, and unknown external outcomes are explicit
   and testable.
6. Public values/errors/pages/audit/exports and domain state contain no OAuth
   secret, raw provider payload/error, vault handle, code/state/PKCE value, or
   unnecessary authorizer PII.
7. Same provider IDs across workspaces never share metadata, credentials,
   intents, cursors, idempotency, errors, reauthorization, or cascades.
8. Revoked credentials fail closed; disconnect/workspace deletion prevent use
   and erase vault/ephemeral slots even when provider revocation is unconfirmed.
9. Disconnect retains `channel-data` by design; explicit channel/workspace data
   deletion remains separately authorized and verified.
10. Exact intent, replay, idempotency, audit, cleanup, and deletion retention
    boundaries are exercised without silently purging unresolved operations.
11. Existing workspace, channel-data, subscriber analytics, CLI, and Notebook
    behavior remains unchanged.
12. The first implementation uses only deterministic fakes/fixtures and the
    standard library: no provider/network/browser/database/KMS/dependency,
    credential, or real data is introduced.
13. Focused/regression tests, compile, Notebook cells, Markdown fences, public
    interface inspection, staged secret scan, graph review, and `git diff
    --check` pass with no unresolved Critical or Required finding.

## Approval status

The user approved all seven choices on 2026-08-20. No technical question is
intentionally left for the reference implementation plan. Choices that remain
outside this slice still require their own later specification and approval,
including the production Web stack, persistence, vault/KMS, provider adapter,
deployment, and the detailed non-engineer UI design.

## Approved review decisions

The following seven choices are approved:

1. Members can read safe connection metadata via `channel.read`; only owners can
   connect, complete callbacks, reauthorize, or disconnect via
   `channel.manage_connection`.
2. Hosted authorization uses only Web Server Authorization Code flow with exact
   HTTPS redirect, one-time session-bound state, PKCE S256, and 10-minute intent
   expiry; desktop/device/implicit flows are excluded.
3. The only scope is `youtube.readonly`; a refresh token, exactly one
   `channels.list(mine=true)` channel, and a successful
   `subscriptions.list(mySubscribers=true)` probe are mandatory.
4. Connections and credential slots are workspace-owned and isolated. The same
   YouTube channel may be independently connected in multiple workspaces but at
   most once actively within one workspace.
5. Production credentials must live in an approved vault/KMS behind opaque
   handles. The standard-library implementation uses synthetic in-memory fakes
   only and exposes no credential-return API.
6. Disconnect immediately removes credential access and metadata but retains
   previously collected `channel-data`; data erasure is a separate explicit
   operation. Removing the original local authorizer does not disconnect a
   workspace-owned shared connection.
7. This slice implements contracts, deterministic fakes, lifecycle behavior,
   privacy cascades, and tests only—no Web routes, provider calls, SDK changes,
   database, jobs, encryption, real credentials, or real channel data.

## Approved implementation clarifications

The reference implementation was approved on 2026-08-20 together with the
following eight clarifications. They refine, and do not weaken, the seven
approved review decisions above.

1. **Detected credential invalidation has a named seam.**
   `ConnectionManager.report_credential_invalidation(context, command)` requires
   `channel.manage_connection`, deletes the credential slot, publishes
   `REAUTH_REQUIRED`, and emits `CONNECTION_REAUTH_REQUIRED`. It accepts no
   provider payload, error text, or token. Without it, success criterion 8 and
   the `CONNECTION_REAUTH_REQUIRED` audit action were unreachable.
2. **Reauthorization completes through `complete_authorization`.** The intent
   already carries operation `REAUTHORIZE` and its target connection id, so no
   separate completion method exists.
3. **`IDEMPOTENCY_CONFLICT` joins the initial error codes.** A mutation key
   reused with a different canonical payload is a conflict, not invalid input,
   so a later HTTP mapping can distinguish 409 from 400. `CALLBACK_CONFLICT`
   remains specific to callback replay.
4. **`EphemeralSecretStore` gains `peek`, and the authorization URL is an
   ephemeral secret.** The URL embeds the one-time state value, so retaining it
   in domain state or in a 90-day idempotency record would defeat digest-only
   state storage. It is stored in the ephemeral secret store, read back for
   exact begin replay, and deleted with the intent. A regression test asserts
   the raw state never appears in service state.
5. **The verified credential is written to its deterministic vault slot
   immediately after exchange.** Every later rejection path then revokes through
   the approved `revoke(workspace_id, credential_slot_id)` port and deletes the
   slot, instead of discarding an unrevoked grant that the specification
   requires to be revoked.
6. **The gateway signals failure with exactly two exceptions.**
   `ProviderRejected(reason)` carries a safe `AuthorizationFailureReason` enum;
   `ProviderUnavailable` marks an unknown outcome, which records a secret-free
   cleanup entry, stays retryable, and publishes nothing. No provider text ever
   crosses the boundary.
7. **The redacted secret wrapper reuses `workspace_access.models.AccessSecret`**
   rather than adding a second implementation of the same guarantee.
8. **`EphemeralSecretStore` and `CredentialVault` expose slot listing.** The
   specification requires a retention reconciler for expired ephemeral slots and
   unreferenced credential slots, which is impossible without enumeration. The
   listing returns slot identifiers only and never credential material.

Pagination limits, ordering, retention boundaries, the permission matrix, the
scope constant, connection ownership, and disconnect-versus-data-deletion
behavior are unchanged.
