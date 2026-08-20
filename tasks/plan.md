# Implementation Plan: channel-connections

Status: proposed
Date: 2026-08-20
Specification: [`SPEC-channel-connections.md`](../SPEC-channel-connections.md)
Capability: [`channel-connections`](../CAPABILITY_MAP.md)

## Overview

Implement the approved tenant-scoped channel-connections contracts as a
deterministic standard-library Python reference backed by in-memory state,
fixed clock/token sources, a strict fake YouTube gateway, and fake secret
stores. The slice proves authorization-intent binding, one-time state, PKCE
S256, atomic callback claim, provider verification gates, credential custody
through opaque slots, connection uniqueness, reauthorization, disconnect
ordering, bounded pagination, retention, and workspace cascades without
choosing a Web framework, HTTP client, Google SDK version, database, queue,
KMS, or vault product.

No real OAuth request, provider call, credential, network access, browser,
persistence, or channel data is introduced. Every secret in this slice is a
deterministic synthetic fixture value.

## Architecture decisions

- `channel_connections.models` owns immutable/slotted public values, commands,
  stable enums, page values, and boundary validation. It imports no HTTP,
  provider SDK, database, filesystem, or analytics type.
- `channel_connections.errors` owns the stable safe error contract with the
  twelve approved machine codes, mirroring `channel_data.errors`.
- `channel_connections.ports` exposes the public `ConnectionReader`,
  `ConnectionManager`, and `ConnectionPrivacyAdministrator` protocols plus the
  deterministic `Clock`, `TokenGenerator`, `EphemeralSecretStore`,
  `CredentialVault`, `YouTubeAuthorizationGateway`, and audit sink boundaries.
- `RedactedSecret`, `ProviderCredential`, and `VerifiedProviderGrant` are
  internal values excluded from the package root. Their `str`/`repr` are always
  redacted and no public method returns credential material.
- `ChannelConnectionsService` is the public in-memory facade. One reentrant
  lock encloses permission checks, idempotency claims, intent creation, the
  atomic callback claim, uniqueness reservation, publication, rotation,
  disconnect, retention, and cascades.
- `channel_connections.memory` holds private typed state: intents indexed by
  state digest, consumed-callback replay records, connections, the active
  uniqueness index, cursor records, idempotency records, cleanup records, and
  the deterministic fake ephemeral/vault stores.
- Tenant keys always start with `context.workspace_id`. A connection, intent,
  cursor, idempotency record, or secret slot is never fetched globally and
  authorized afterward.
- Provider configuration (authorization host, redirect URI identifier, scope
  set) is server-side trusted constant data. No command or callback value can
  influence a URL, host, scope, workspace, user, or channel selection.
- Credential slot identifiers are deterministic from internal opaque intent and
  connection identifiers so retries cannot create unbounded orphan copies.
- The fake gateway and fake stores are named and documented as fakes. They are
  not encryption and make no security claim.

## Interface-sequencing clarifications resolved by this plan

The approved specification defines the callback gates and the
`REAUTH_REQUIRED` status but does not name the operation that records a
detected credential invalidation, even though success criterion 8 and the
`CONNECTION_REAUTH_REQUIRED` audit action both require it. This plan resolves
two such gaps; approving this plan approves both clarifications.

1. `ConnectionManager` gains
   `report_credential_invalidation(context, command)` requiring
   `channel.manage_connection`. It deletes the credential slot, publishes
   `REAUTH_REQUIRED`, emits `CONNECTION_REAUTH_REQUIRED`, and is the only seam
   a future execution authority uses to report a revoked or expired grant. It
   accepts no provider payload, error text, or token value.
2. Reauthorization completes through the existing `complete_authorization`
   entry point, because the intent already carries operation `REAUTHORIZE` and
   its target connection identifier. No separate `complete_reauthorization`
   method is added.

## Dependency graph

```text
Immutable contracts, safe errors, redacted secrets, and protocols
                              |
                              v
        Tenant authorization + idempotency + intent creation
                              |
                              v
     Atomic callback claim, provider verification, publication
                              |
                   +----------+----------+
                   |                     |
                   v                     v
        Reads and bounded pagination   Reauthorization + rotation
                   |                     |
                   +----------+----------+
                              v
              Disconnect, revocation, and invalidation
                              |
                              v
             Tenant isolation proofs across every operation
                              |
                              v
              Retention, cascade, audit, and export safety
                              |
                              v
              Integration, documentation, and final review
```

## Implementation sequence

### Phase 1: Contract and authorization foundation

1. Add immutable public values, commands, stable enums and errors, redacted
   secret wrappers, provider grant values, the port protocols, and the
   intentionally small package root.
2. Add the tenant-scoped in-memory facade with exact permission checks,
   non-enumerating lookups, actor/payload-bound idempotency, and
   `begin_authorization` intents carrying one-time state digests, PKCE S256
   challenges, fixed scope/redirect constants, and 10-minute expiry.
3. Add `complete_authorization`: state-digest lookup, fresh binding comparison,
   atomic one-time claim, PKCE consumption, fake gateway exchange, exact-scope
   and refresh-token gates, single `mine=true` channel requirement,
   `mySubscribers=true` capability probe, uniqueness reservation, vault put,
   atomic publication, ephemeral cleanup, denial handling, and 24-hour exact
   replay.

Checkpoint A: contract, intent, and callback tests pass; no duplicate exchange
occurs under concurrency and no provider detail reaches a public value.

### Phase 2: Connection lifecycle

4. Add `get_connection` and `list_connections` with bounded deterministic
   pagination: limits 1-100, default 50, `connected_at` descending then
   `connection_id` ascending, and opaque cursors bound to workspace, ordering,
   limit, and a captured immutable result.
5. Add `begin_reauthorization` and its completion path: exact provider channel
   match, atomic credential rotation, old-slot deletion after the new revision
   commits, `REAUTH_REQUIRED` to `ACTIVE` recovery, and mismatch failure that
   preserves the current credential.
6. Add `disconnect` in fail-closed order, provider-revocation uncertainty as a
   secret-free cleanup record, `report_credential_invalidation`, and proof that
   collected `channel-data` is untouched.

Checkpoint B: connection lifecycle, rotation, disconnect, and invalidation
tests pass; racing connect, reauthorize, and disconnect never overwrite or
resurrect a credential slot.

### Phase 3: Isolation and privacy lifecycle

7. Add explicit tenant isolation fixtures using identical connection, channel,
   intent, cursor, secret-slot, and idempotency identifiers in two workspaces
   across every public operation, and fix any leak the fixtures expose.
8. Add exact retention cutoffs (10-minute intents and PKCE slots, 24-hour
   replay records, 90-day terminal idempotency and resolved cleanup records),
   orphan ephemeral/vault slot reconciliation, `delete_workspace_connections`,
   secret-free audit events, and export allowlist inspection.

Checkpoint C: full channel-connections, workspace-access, channel-data, and
subscriber analytics suites pass; no addressable tenant copy or secret survives
deletion and no unresolved operation is silently purged.

### Phase 4: Integration and completion

9. Prove a ready connection reference resolves through real
   `workspace_access` contexts for a `channel-data` consumer, document safe
   usage and limits in the module README, run graph-backed, five-axis,
   security, and simplification review, fix only concrete findings, and update
   durable progress.

Final checkpoint: every approved success criterion has direct evidence and no
Web route, provider call, SDK, database, job, UI, dependency, real credential,
or real channel data has been introduced.

## Verification commands

```powershell
# Focused channel-connections suite
python -m unittest discover -s channel_connections/tests -v

# Existing authorization, channel data, and analytics regressions
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q channel_connections workspace_access channel_data subscriber_analytics

# Notebook JSON/code-cell validation
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"

# Patch integrity
git diff --check
```

There is no configured formatter, linter, type checker, CI workflow, or native
dependency audit. No dependency is added in this plan. Final review also checks
Markdown fences, staged credential values, public-record mutability, redacted
secret rendering, and a fixture runtime path.

## TDD and checkpoint policy

- Every behavior follows red-green-refactor. Record the expected RED failure
  before writing the corresponding production behavior.
- Run the focused test file after each red/green cycle and the full
  channel-connections suite after refactoring.
- Run workspace-access, channel-data, and subscriber analytics regressions
  before every implementation commit.
- Keep contracts, intents, callback, reads, reauthorization, disconnect,
  isolation, privacy, and final documentation in separate verified commits.
- Build/update the knowledge graph before exploration and after each coherent
  source milestone. If the graph remains unregistered or empty, record that
  limitation rather than reporting it as coverage evidence.
- Stage only explicit task paths. Push clean verified milestones to
  `origin/feature/multi-channel-analytics`.
- Update `.agents/progress/youtube-analysis-app.md` after each checkpoint.

## Definition of Done application

Each task must satisfy its acceptance criteria plus the standing Definition of
Done: focused behavior is runtime-tested, existing suites pass, edge and error
paths are covered, no unrelated changes or duplicate logic remain, public
contracts are documented, integration compatibility is considered, and
security/privacy implications are reviewed. Missing formatter, type-checker,
and CI commands are documented project constraints, not silently claimed
checks.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A callback value is trusted as authority | Critical | Accept only bounded code/state/provider-error plus the server session; resolve workspace, user, redirect, scope, and channel from trusted server state and the verified grant |
| Two exchanges run for one intent | Critical | Hash-indexed lookup, atomic one-time claim under the service lock before any exchange, and a stable retryable conflict for in-flight duplicates |
| A secret reaches a record, error, audit event, or log | Critical | Redacted wrappers, digest-only state/code storage, opaque vault slots, allowlisted public fields, and an explicit representation-inspection test |
| A fake store is mistaken for secure storage | High | Name, document, and test the fakes as synthetic in-memory fixtures; keep the production vault/KMS decision an explicit later gate |
| Wrong or ambiguous YouTube channel is connected | High | Require exactly one `mine=true` channel, a successful `mySubscribers` probe, and an exact provider-channel match on reauthorization |
| Credential orphans accumulate on failure | High | Deterministic slots from internal identifiers, cleanup records for unknown external outcomes, and orphan reconciliation in retention |
| Same provider channel collides across tenants | High | Prefix every connection, intent, cursor, idempotency, cleanup, and secret key with workspace and exercise identical-ID cross-tenant fixtures |
| Rotation or disconnect races corrupt credentials | High | Serialize each transition under one lock, publish rotation atomically, and delete the old slot only after the new revision commits |
| Unknown provider timeout is treated as failure | High | Record explicit unresolved cleanup state, keep the operation retryable by deterministic identifier, and never purge unresolved records |
| Cursor leaks or crosses tenants | Medium | Server-side opaque cursor records bound to workspace, ordering, limit, and captured result, with one safe invalid error |
| In-memory lock semantics are mistaken for database guarantees | Medium | Document transactional obligations for the later persistence slice and keep all persistence artifacts out of this package |
| Service file grows unwieldy | Medium | Keep models, errors, ports, memory state, and orchestration separated and review size at each checkpoint; split only around a proven cohesive seam |

## Scope discipline

Intentionally untouched:

- Web framework, HTTP routes, callback pages, cookies, CSRF middleware, rate
  limiting, security headers, CORS, frontend, UI, and localization.
- Real Google OAuth, consent-screen verification, client registration, provider
  HTTP/SDK code, quota use, and any real channel connection.
- Database schema, migrations, ORM, cache, queue, background jobs, scheduling,
  and credential lease/execution authority.
- Managed KMS/vault selection, encryption implementation, key rotation, backup
  recovery, and legal holds.
- `channel_data` retention/coverage rules, `analytics_core` behavior, existing
  CLI flags, CSV columns, Japanese labels, and Notebook controls.
- The local CLI `token.json` and environment OAuth JSON, which are never read,
  copied, imported, or migrated.

## Open questions

Both interface gaps identified above are resolved by this plan:
`report_credential_invalidation` is the named seam for detected revocation, and
reauthorization completes through `complete_authorization`. Human approval of
this plan confirms those clarifications together with the task ordering and
checkpoint scope. They will be copied back into the living specification before
code is written.
