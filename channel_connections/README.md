# channel-connections

Tenant-scoped contracts for connecting, inspecting, reauthorizing, and
disconnecting YouTube channels without exposing OAuth credentials to browsers,
application records, logs, other workspaces, or analytics consumers.

Specification: [`SPEC-channel-connections.md`](../SPEC-channel-connections.md)

## What this module is

A deterministic standard-library reference implementation backed by in-memory
state. It proves the security and lifecycle invariants before a Web framework,
HTTP client, Google SDK, database, queue, or credential vault is chosen.

**It performs no real OAuth request, provider call, or network access, stores no
real credential, and reads no local `token.json` or environment OAuth JSON.**
The `subscriber_analytics` CLI and Notebook keep their own local desktop flow;
nothing here imports or migrates it.

## Public surface

```python
from channel_connections import (
    ChannelConnectionsError,
    ConnectionManager,
    ConnectionPrivacyAdministrator,
    ConnectionReader,
    ErrorCode,
)
from channel_connections.service import ChannelConnectionsService
```

Immutable values and commands live in `channel_connections.models`. The
credential-boundary values (`RedactedSecret`, `ProviderCredential`,
`VerifiedProviderGrant`) are internal, always render redacted, and are excluded
from the package root. No public method returns credential material.

## Permissions

Every adapter authenticates its own session and resolves a fresh
`workspace_access.WorkspaceContext` before calling this module. The module
checks the exact permission itself and never infers authority from a role,
route parameter, provider token, or serialized permission set.

| Operation | Permission |
|---|---|
| `get_connection`, `list_connections` | `channel.read` |
| `begin_authorization`, `complete_authorization` | `channel.manage_connection` |
| `begin_reauthorization` | `channel.manage_connection` |
| `disconnect`, `report_credential_invalidation` | `channel.manage_connection` |
| `purge_retention` | `channel.manage_connection` |
| `delete_workspace_connections` | `workspace.delete` |

## Authorization lifecycle

1. `begin_authorization` creates a one-time intent bound to the current
   workspace, user, and session. It stores only a SHA-256 digest of the state
   value, holds the PKCE verifier in the ephemeral secret store, sends the fixed
   `youtube.readonly` scope and configured redirect identifier, and expires at
   exactly 10 minutes.
2. The returned `AuthorizationStart` carries only the intent id, the gateway
   authorization URL, and the expiry. The URL must be HTTPS on an allowlisted
   provider host; anything else fails closed. Because the URL embeds the
   one-time state, it is kept in the ephemeral secret store and deleted with the
   intent rather than retained in domain state.
3. `complete_authorization` accepts only a bounded state, an authorization code
   or a safe provider error code, and the idempotency key. It never accepts a
   workspace, user, connection, redirect, scope, or channel from the browser.
4. Completion hashes the state, requires an unexpired intent, compares the
   workspace, user, and session bindings, and claims the intent atomically
   before any exchange. A duplicate in flight receives a retryable conflict and
   never triggers a second exchange.
5. A grant is accepted only with exactly the approved scope set, a refresh
   token, one verified provider channel, and a successful subscriber capability
   probe. Any rejection revokes the grant and deletes its credential slot.
6. Publication reserves the tenant-scoped active key
   `(workspace_id, provider, provider_channel_id)` and writes safe metadata that
   references the credential slot only internally.

An exact duplicate callback replays the original connection for 24 hours; a
changed actor, session, state, code, or payload fails as `CALLBACK_CONFLICT`.

## Reauthorization, disconnect, and invalidation

- `begin_reauthorization` names one existing connection and reuses every gate.
  The verified provider channel must match exactly; a mismatch fails as
  `REAUTH_CHANNEL_MISMATCH` and leaves the current credential untouched.
- A successful rotation keeps the connection id and `connected_at`, commits the
  new credential slot, then deletes the previous slot.
- `disconnect` marks the connection unavailable, asks the gateway to revoke,
  deletes the credential slot whether or not revocation is confirmed, removes
  the metadata and active key, and keeps only secret-free evidence.
- `report_credential_invalidation` is the seam a future collection executor uses
  to report a revoked or expired grant. It deletes the slot and publishes
  `REAUTH_REQUIRED`. It accepts no provider payload, error text, or token.

**Disconnect does not delete `channel-data`.** Collected observations are
removed only by that module's own authorized deletion or workspace cascade.

## Reading and pagination

`list_connections` returns pages of 1-100 items (default 50) ordered by
`connected_at` descending then `connection_id` ascending. Cursors are opaque,
single use, and bound to workspace, query, and the workspace revision captured
when the page was produced. Tampered, foreign, and cross-query tokens share one
`INVALID_CURSOR` error and do not consume the original cursor; a changed result
set fails as `CURSOR_EXPIRED` rather than mixing revisions.

## Retention and deletion

| Record | Boundary |
|---|---|
| Unclaimed intent and its PKCE/URL slots | exactly 10 minutes |
| Consumed callback replay record | 24 hours |
| Mutation idempotency records | 90 days |
| Unresolved cleanup records | never purged automatically |
| Connection metadata and credential slot | until disconnect or workspace deletion |

`purge_retention(context, reference_time)` is workspace-scoped and also removes
expired ephemeral slots and unreferenced credential slots. It reports only safe
counts. `delete_workspace_connections` removes every copy for one workspace and
leaves identically named foreign records intact.

## Errors

Stable codes: `INVALID_INPUT`, `PERMISSION_DENIED`,
`CONNECTION_NOT_FOUND_OR_FORBIDDEN`, `CONNECTION_ALREADY_EXISTS`,
`INTENT_NOT_FOUND_OR_EXPIRED`, `CALLBACK_CONFLICT`, `IDEMPOTENCY_CONFLICT`,
`OPERATION_IN_PROGRESS`, `PROVIDER_AUTHORIZATION_FAILED`,
`PROVIDER_CAPABILITY_MISSING`, `REAUTH_CHANNEL_MISMATCH`, `INVALID_CURSOR`,
`CURSOR_EXPIRED`.

Errors expose only a code, a stable generic message, an optional safe field, a
retryable flag, an opaque correlation id, and an optional safe reason code.
Missing and foreign resources are indistinguishable. Provider status codes,
response bodies, candidate channel lists, token timings, and vault identifiers
are never public.

## Gateway and store boundaries

The gateway owns provider endpoints, transport, TLS, timeouts, schema
validation, and revocation. It signals failure with exactly two exceptions:

- `ProviderRejected(reason)` — a definitive refusal carrying a safe reason enum
  (`PROVIDER_DENIED`, `CHANNEL_NOT_UNIQUE`, `SCOPE_NOT_GRANTED`, …).
- `ProviderUnavailable` — an unknown outcome such as a timeout. The service
  records a secret-free cleanup entry, stays retryable, and publishes nothing.

## In-memory limits and production gates

`InMemoryEphemeralSecretStore` and `InMemoryCredentialVault` are **synthetic
fakes**. They are not encryption, not durable, and make no security claim.
Production requires an approved managed vault or KMS behind the same ports, key
rotation, access auditing, backup erasure, and a documented recovery procedure.
Lookups are linear scans suited to fixtures only; persistence, transactions,
indexes, Web routes, CSRF and rate limiting, background execution authority, and
Google OAuth verification belong to later approved slices.

## Verification

```powershell
python -m unittest discover -s channel_connections/tests -v
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q channel_connections workspace_access channel_data subscriber_analytics
git diff --check
```

The repository has no configured formatter, linter, type checker, or CI
workflow, so those checks are not claimed.
