# YouTube analysis app progress

Last updated: 2026-08-21

## Objective and scope

Evolve `subscriber_analytics` from a single-channel CLI/Notebook into a hosted,
multi-user, multi-channel YouTube analysis application. The UI must expose
analysis filters such as subscription period, comment activity, subscriber
status, and segment without duplicating the analytics logic.

Current working assumption: start with shared analytics/application modules,
then add the hosted web surface. The end state is a hosted multi-user product,
because the user asked for use by many channel owners. Do not start OAuth or
collect real channel data without an explicit owner-authorized run.

## Verified milestone: safe collection foundation

Branch: `feature/multi-channel-analytics`

Base HEAD: `0d565f99af1ab9626e0e0fa5dd1557be328a8da5`

The pending milestone hardens the existing CLI/Notebook before app extraction:

- adds a secret-safe preflight for OAuth source, channel identity, and
  `mySubscribers` access;
- supports Codespaces OAuth JSON secrets without launching loopback OAuth;
- stops subscriber collection when the OAuth channel differs from the expected
  channel;
- records comment collection coverage and fails closed when videos are missing;
- distinguishes owner OAuth coverage from API-key public-only coverage, and
  rejects public-only data for silent-subscriber classification by default;
- keeps CLI and Notebook safety behavior aligned;
- adds regression tests and setup documentation.

Relevant paths:

- `subscriber_analytics/preflight.py`
- `subscriber_analytics/common.py`
- `subscriber_analytics/collect_subscribers.py`
- `subscriber_analytics/collect_comments.py`
- `subscriber_analytics/extract_silent.py`
- `subscriber_analytics/subscriber_analytics.ipynb`
- `subscriber_analytics/tests/test_analytics.py`
- `subscriber_analytics/README.md`

Verification evidence:

- `python -m unittest discover -s subscriber_analytics/tests -v` — 11 passed.
- `python -m compileall -q subscriber_analytics` — passed.
- Notebook JSON parse and every code-cell Python compile — passed.
- `git diff --check` — passed; only existing Windows LF/CRLF notices.
- source-tree OAuth client/API-key pattern scan — no matches.
- code-review-graph incremental rebuild — 66 nodes / 801 edges, no parse errors.

## Verified milestone: analytics-core Checkpoint A

Branch: `feature/multi-channel-analytics`

Verified HEAD: `761ea34b8d091db3ef9f0781ce7c04697ad2d829`

Completed commits:

- `10bab3b` approves the ordered analytics-core task breakdown.
- `ca2226f` adds the immutable pure core contract and default four-segment
  analysis, including silent totals and the public-subscriptions limitation.
- `761ea34` adds every approved filter, UTC boundary rule, typed validation,
  empty-input handling, and permutation-independent ordering.

Verification evidence at this checkpoint:

- focused core suite: 14 tests passed;
- full subscriber analytics suite: 25 tests passed;
- `python -m compileall -q subscriber_analytics`: passed;
- `git diff --check`: passed;
- code-review graph: 44 nodes and 618 edges updated with no parse errors;
- graph search found all 14 core test nodes and direct tests for `analyze`.

No OAuth flow, YouTube API collection, production data, database, HTTP API, UI,
or new dependency was used or added. CLI and Notebook migration remain pending.

## Verified milestone: analytics-core complete

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `253614eeb8c216f931412bdcbb5b983a96115213`

Completed implementation and migration commits after Checkpoint A:

- `2e9d382` routes the CLI through `analytics_core.analyze` and adds
  fixture-backed CSV/silent-filter regressions.
- `4ea6492` routes the Notebook through the same entry point and verifies every
  code cell compiles without legacy calculation calls.
- `6208729` removes the four superseded calculation helpers after graph queries
  reported zero callers.
- `253614e` documents the current shared-engine behavior, silent definitions,
  coverage gate, filter boundaries, and public-only limitation.

Final verification evidence:

- focused analytics-core suite: 14 tests passed;
- full subscriber analytics suite: 29 tests passed;
- fixture-backed CLI runs covered default output, `--never-commented`, and
  `--no-comment-within 90d` without network or credentials;
- `python -m compileall -q subscriber_analytics`: passed;
- Notebook JSON and every code cell: parsed and compiled;
- `extract_silent.py --help`: matched the documented CLI flags;
- `git diff --check` and credential-value scan: passed;
- graph rebuilds completed without parser errors;
- graph callers show CLI and Notebook using `analytics_core.analyze`; old
  calculation helpers had zero callers before deletion;
- graph change review found no affected registered flows. Its conservative
  static test-gap warning does not resolve helper-mediated tests, while all 14
  core behavior cases and 29 total tests executed successfully.

Five-axis review verdict: no unresolved Critical or Required findings.
Correctness is covered at UTC/filter boundaries and adapter fixtures; the core
has no I/O or external dependencies; external CSV data is normalized before a
strict typed boundary; analysis is linear plus deterministic sorting; no OAuth,
collector, dependency, database, HTTP, or UI behavior changed.

Remaining product constraints are intentional: YouTube exposes only public
subscriptions, complete owner-scope comments are required by default, and no
real OAuth/API/production-data run was performed. The repository still has no
configured formatter, linter, type checker, or CI workflow.

## Verified milestone: workspace-access session boundary

Branch: `feature/multi-channel-analytics`
Verified implementation HEAD: `72ff6a0`

- `4b8f661` approves the workspace-access specification, implementation plan,
  and six-task TDD checklist after the user's continue-to-completion direction.
- `ec95d30` adds immutable/slotted access records, stable errors, redacted secret
  wrappers, UTC validation, and the exact `OWNER`/`MEMBER` permission matrix.
- `72ff6a0` adds verified-identity mapping and secure in-memory sessions with
  one-time raw-secret issuance, SHA-256 digest-only retention, 30-minute idle
  and 12-hour absolute expiry, revocation, logout-all, and suspension behavior.

Verification evidence:

- workspace-access suite: 16 tests passed (6 contract, 10 session/port);
- subscriber analytics regression suite: 29 tests passed;
- `python -m compileall -q workspace_access subscriber_analytics`: passed;
- `git diff --check` and credential-value scan: passed;
- graph rebuild: 53 new/changed nodes, 391 edges, no parser errors;
- staged graph review found no affected existing flow. Static gaps were limited
  to protocol/private helper attribution; production clock/token defaults now
  have direct tests and all public session operations execute in behavior tests.

No database, dependency, HTTP/cookie adapter, CSRF implementation, OAuth,
authentication provider, credential, real user data, or production behavior was
introduced. Next implementation slice is workspace creation/selection/context
resolution, followed by membership and atomic last-owner safety.

## Verified milestone: workspace-access tenant isolation

Verified implementation HEAD: `1ef4423`

- `6745e6f` adds atomic workspace/first-owner creation, stable tenant-scoped
  listing, explicit/single/preferred selection, current-role permission checks,
  and non-enumerating foreign/missing workspace failures.
- `1ef4423` adds owner-only membership grant/change/revoke, exact-payload
  idempotent replay, key-conflict rejection, current authorization revisions,
  and serial/concurrent last-owner protection under the same state lock.

Verification evidence:

- workspace-access suite: 31 tests passed, including explicit cross-tenant
  negative fixtures and two-thread owner-departure concurrency;
- subscriber analytics regression suite: 29 tests passed;
- compile, `git diff --check`, and credential-value scan passed;
- graph rebuild completed without parser errors and found no affected registered
  existing flow. Its private-helper coverage warning is conservative: public
  session, workspace, and membership operations execute through those helpers
  in focused behavior tests.

Tenant selectors remain untrusted hints, missing/foreign identifiers share safe
errors, contexts are single-workspace and current-revision bound, and no
database, HTTP, OAuth, dependency, real data, or production adapter was added.

## Verified milestone: workspace-access complete

Verified implementation HEAD: `38b3b57`

Completed final slices:

- `114cc20` adds allowlisted access export, sole-owner deletion preflight,
  session revocation/tombstoning, 30/90/365-day retention, and secret-free audit
  events.
- `d8cfe1f` separates private in-memory state/audit collection and stable error
  constructors from the public orchestration service without behavior changes.
- `38b3b57` closes final review findings: owner-only workspace update/delete,
  safe deletion replay, malformed external-value validation, generated
  correlation IDs, and idempotency actor removal during account deletion.

Final verification evidence:

- workspace-access suite: 40 tests passed;
- subscriber analytics regression suite: 29 tests passed;
- `python -m compileall -q workspace_access subscriber_analytics`: passed;
- every Notebook code cell parsed and compiled;
- fixture runtime created an owner workspace/context and rendered the session
  value only as `AccessSecret(<REDACTED>)`;
- `git diff --check`, Markdown fence validation, and credential-value scan
  passed;
- graph rebuilt at the verified implementation HEAD with no parser errors;
  public session/context/membership/privacy operations have direct behavior
  tests even where static helper attribution remains conservative.

Five-axis review verdict: no unresolved Critical or Required finding.
Correctness covers exact expiry/retention boundaries, idempotent replay, stale
authorization, and concurrent last-owner changes. Security covers digest-only
session storage, current-role authorization, cross-tenant existence hiding,
runtime input validation, PII allowlists/deletion, and secret-free errors/audit.
The in-memory adapter uses linear scans suitable only for fixtures; production
pagination/indexes belong to later persistence/API modules. No dependency,
database, endpoint, provider/OAuth flow, credential, real user/channel data, or
production deployment was introduced.

## Verified milestone: channel-data contract and tenant boundary

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `da25f80`

Completed commits:

- `b6bd654` defines the stable, safe channel-data error contract.
- `cb8a2ff` adds bounded immutable external input values and UTC normalization.
- `fa78ff0` freezes output, command, page/query, retention, and repository port
  contracts with `WorkspaceContext` on every tenant operation.
- `da25f80` adds the locked in-memory facade for authorized, actor/payload-bound
  idempotent collection starts and identical-ID cross-workspace isolation.

Verification evidence:

- channel-data suite: 16 tests passed;
- workspace-access regression suite: 40 tests passed;
- subscriber analytics regression suite: 29 tests passed;
- `python -m compileall -q channel_data workspace_access subscriber_analytics`:
  passed;
- public protocol signature and two-workspace runtime fixtures: passed;
- `git diff --check` and credential-value scan: passed;
- code-review graph rebuilt without parser errors; no registered flow was
  affected, while its conservative private-helper/static test attribution is
  not treated as executed coverage evidence.

Candidate subscriber/video/comment data remains non-public until an exact
`finish_collection(COMPLETE)` promotion. No database, migration, endpoint,
OAuth, provider call, job, UI, dependency, credential, or real channel data was
introduced. Next work is collection transitions/freshness (Task 3), followed by
subscriber and owner-video/comment accepted generations (Tasks 4-5).

## Verified milestone: channel-data accepted analysis dataset

Verified implementation HEAD: `576138b`

- `1df0ec1` adds monotonic collection transitions, deterministic history, and
  latest-attempt versus latest-accepted freshness.
- `b314f03` stages subscriber snapshots and atomically folds exact COMPLETE
  generations into a cumulative, permutation-independent registry.
- `576138b` stages video inventories and minimized per-video/author comment
  aggregates, promotes only complete exact-inventory coverage, and exposes a
  fail-closed silent-analysis dataset.

Verification evidence:

- channel-data suite: 28 tests passed;
- workspace-access suite: 40 tests passed;
- subscriber analytics suite: 29 tests passed;
- transition/concurrency, out-of-order registry, explicit empty-video,
  stale-inventory, public-scope, partial-replacement, and record allowlist
  fixtures passed;
- all four readiness reasons (`NO_SUBSCRIBER_SNAPSHOT`, `NO_VIDEO_INVENTORY`,
  `PUBLIC_VIDEO_SCOPE_ONLY`, `COMMENTS_INCOMPLETE`) were exercised;
- compile, graph rebuild, credential scan, and `git diff --check` passed.

Only public subscriber observations are represented. Stored comment activity
has no comment text, author display name, reply/parent relation, or comment ID.
Next work is bounded cursor pagination, then exact retention/deletion cascades.

## Verified milestone: channel-data complete

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `f6f8aec3d9b7f4c221067c1f128de24d467ee49a`

Completed final slices:

- `9a799dc` binds one-time snapshot cursors to workspace, query, limit, and
  ordering so traversal cannot cross tenants or mix accepted generations.
- `d07a1c3` enforces exact retention boundaries and complete channel/workspace
  deletion cascades while preserving identical foreign-tenant identifiers.
- `aa7eb88` proves the ready dataset maps losslessly into analytics-core and
  reproduces `NEW_SILENT`, `OLD_SILENT`, `DORMANT`, and `ACTIVE`.
- `f022c1c` separates privacy state operations from service orchestration.
- `fdf64c8` closes retention-cursor leakage and COMPLETE-state invariants.
- `f6f8aec` makes the public `SilentAnalysisDataset` contract itself reject
  incomplete or public-video-only comment coverage.

Final verification evidence:

- channel-data suite: 35 tests passed;
- workspace-access suite: 40 tests passed;
- subscriber analytics suite: 29 tests passed;
- `python -m compileall -q channel_data workspace_access subscriber_analytics`:
  passed;
- Notebook JSON parsed and every code cell compiled;
- analytics integration reproduced all four segments and both mandatory
  public-subscription limitations;
- Markdown fences, public-interface/immutability inspection, forbidden-comment
  field scan, staged credential-value scan, and `git diff --check` passed;
- code-review graph fully rebuilt at the verified implementation HEAD: 41
  files, 648 nodes, 7,171 edges, and no parser errors.

Five-axis review verdict: no unresolved Critical or Required finding.
Correctness includes atomic generation visibility, readiness, pagination,
retention, deletion, concurrency, and exact tenant isolation. Security includes
permission checks on every public operation, non-enumerating errors,
workspace-bound keys/cursors, minimized aggregate-only comment data, and
working retention/deletion paths. Production functions are at most 67 lines;
the public protocols and indirectly called retention method are intentional,
not dead code. Static graph warnings for private helper methods are conservative
and do not replace the executed behavior tests above.

No database, migration, endpoint, OAuth flow, provider call, background job,
UI, dependency, credential, or real channel data was introduced. The next
capability is `channel-connections`; threat-model Web OAuth and encrypted token
storage before implementing it or collecting any real data.

## Decisions and constraints

- OAuth scope remains `youtube.readonly`; no write/delete/upload permission.
- YouTube Studio delegated Editor permissions are not sufficient for
  `mySubscribers`; an API-visible channel owner must authorize.
- Desktop loopback OAuth cannot be completed remotely by sending its URL to a
  different computer. Hosted use requires Web OAuth with an HTTPS callback.
- Refresh tokens are credentials and must eventually live in an encrypted
  credential store/KMS, never source control, logs, chat, or plain application
  tables.
- Public subscriber results are incomplete by YouTube design; only subscribers
  who expose their subscriptions are observable.
- The existing CLI and Notebook must remain adapters over the same analytics
  behavior used by the future UI.

## Next steps

1. Commit the verified safe-collection milestone on the current branch.
2. Write a concise product specification and capability map for the hosted MVP.
3. Record ADRs/threat model for Web OAuth, encrypted credential storage, and
   workspace/tenant isolation before implementing authentication.
4. Extract the pure analytics interface from `extract_silent.py` using golden
   regression tests; keep file/data I/O in adapters.
5. Add a single-channel API/UI vertical slice against fixture data before
   introducing multi-user OAuth or background jobs.

Recommended process skills: `spec-driven-development`, then
`incremental-implementation` + `test-driven-development`; use
`api-and-interface-design` for public contracts and `security-and-hardening`
before OAuth/token persistence.

## Approved checkpoint: channel-connections specification

Branch: `feature/multi-channel-analytics`

Approved specification HEAD: `55a11435c19fcb01c344164f2052a018ffcd375e`

On 2026-08-20 the user approved all seven decisions in
`SPEC-channel-connections.md`:

1. members read safe metadata while owners manage connections;
2. hosted Web Authorization Code flow uses exact HTTPS redirect, one-time
   session-bound state, PKCE S256, and 10-minute intents;
3. only `youtube.readonly` is allowed, with refresh-token, `mine=true`, and
   `mySubscribers=true` verification;
4. connections and credentials are workspace-owned and tenant-isolated;
5. production credentials require an approved vault/KMS while the reference
   slice uses synthetic in-memory fakes only;
6. disconnect removes credential access but retains collected `channel-data`,
   and authorizer departure does not disconnect a workspace-owned connection;
7. the reference slice excludes Web/UI, real OAuth/provider calls, database,
   KMS, jobs, credentials, and real data.

No `channel-connections` implementation has started. No dependency, OAuth
configuration, credential, provider call, real channel data, Web route, or UI
was introduced during specification approval.

The final hosted product now explicitly requires a clear UI that a
non-engineer can operate: guided connection steps, plain Japanese labels,
visible status and next actions, safe actionable errors, accessibility, and
responsive behavior. This is a future Web/UI acceptance requirement, not part
of the approved reference implementation slice.

The unrelated untracked `.repowise/wiki.db` was confirmed as an unreferenced,
reproducible RepoWise SQLite cache and removed before this checkpoint.

### Next session

1. Break the approved reference slice into small ordered implementation tasks.
2. Implement its deterministic domain contracts and synthetic fakes test-first,
   without network, browser, database, KMS, credentials, or real data.
3. Run focused and full regression suites, compile, secret scans, Markdown
   validation, graph impact review, and `git diff --check`.
4. Specify the later frontend separately with the non-engineer UI acceptance
   requirements, then verify it in a real browser when UI work is authorized.

Recommended next-session skills: `planning-and-task-breakdown`, then
`incremental-implementation` and `test-driven-development`; use
`api-and-interface-design` and `security-and-hardening` for their distinct
domain responsibilities. Use the code-review graph before codebase exploration.

## Verified milestone: channel-connections Checkpoint A

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `51d51a4`

Completed commits:

- `5734a33` breaks the approved reference slice into nine ordered TDD tasks with
  three checkpoints and ignores the reproducible local RepoWise cache.
- `1c2f915` freezes immutable connection, authorization-start, page, audit, and
  retention values, the stable safe error contract, bounded callback commands,
  the internal credential-boundary values, and every port protocol.
- `294d3e7` adds the tenant-scoped service seam and `begin_authorization`:
  exact permission checks, one lock per transition, actor/session/payload-bound
  idempotency, state digests, PKCE S256, fixed scope/redirect configuration, and
  10-minute expiry.
- `51d51a4` adds `complete_authorization` with the approved callback gates,
  provider verification, deterministic credential slots, atomic publication,
  revocation of rejected grants, and 24-hour exact replay.

Verification evidence at this checkpoint:

- channel-connections suite: 78 tests passed;
- workspace-access suite: 40 tests passed;
- channel-data suite: 35 tests passed;
- subscriber analytics suite: 29 tests passed;
- `python -m compileall -q channel_connections workspace_access channel_data
  subscriber_analytics`: passed;
- `git diff --check` and a staged credential-shaped-value scan: passed;
- concurrency fixtures proved one intent per idempotency key and exactly one
  provider exchange for concurrent callbacks.

Design decisions taken during implementation, to be reflected in the living
specification at Task 9:

1. `ConnectionManager.report_credential_invalidation` is the named seam for a
   detected revoked or expired grant; it carries no provider payload.
2. Reauthorization completes through the existing `complete_authorization`,
   because the intent already carries its operation and target connection.
3. `IDEMPOTENCY_CONFLICT` joins the initial error codes so a reused mutation key
   is distinguishable from invalid input for a later HTTP mapping.
4. `EphemeralSecretStore` gains `peek`. The authorization URL embeds the
   one-time state, so it is stored as an ephemeral secret and deleted with the
   intent instead of being retained in domain state or the 90-day idempotency
   record. A test asserts the raw state never appears in service state.
5. The verified credential is written to its deterministic vault slot
   immediately after exchange, so every later rejection path can revoke through
   the approved `revoke(workspace_id, credential_slot_id)` port and delete the
   slot rather than discarding an unrevoked grant.
6. `ProviderRejected(reason)` and `ProviderUnavailable` are the gateway's only
   failure contract. A rejection carries a safe reason enum; an unknown outcome
   keeps a secret-free cleanup record, stays retryable, and never publishes.
7. The redacted secret wrapper reuses `workspace_access.models.AccessSecret`
   rather than adding a second implementation.

The implementation plan and task list are still recorded as `proposed`. Human
approval of those seven decisions and of the nine-task ordering is the open
gate; no further specification change was made.

No Web route, HTTP adapter, real OAuth call, Google SDK, database, migration,
job, UI, dependency, real credential, or real channel data was introduced.
Remaining tasks are pagination, reauthorization, disconnect and invalidation,
tenant isolation proofs, retention and cascades, then integration review.

## Verified milestone: channel-connections complete

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `bc3497d`

Completed slices after Checkpoint A:

- `8d44fa7` records the user's approval of the nine-task ordering and the seven
  Checkpoint A decisions.
- `e16ece3` adds bounded pagination with single-use opaque cursors bound to
  workspace, query, and the captured workspace revision.
- `3b5b555` adds reauthorization with exact provider-channel matching and
  credential rotation that commits the new slot before deleting the old one.
- `9c07fb9` adds workspace-scoped retention at the exact 10-minute, 24-hour, and
  90-day boundaries, orphan slot reconciliation, and the workspace cascade.
- `d8974ff` proves tenant isolation under a worst-case colliding token
  generator; no production change was required.
- `bc3497d` adds the real-context integration suite, the module README, the
  eight approved specification clarifications, one function split, and removal
  of two unused helpers.

Final verification evidence:

- channel-connections suite: 131 tests passed;
- workspace-access suite: 40 tests passed;
- channel-data suite: 35 tests passed;
- subscriber analytics suite: 29 tests passed;
- `python -m compileall -q channel_connections workspace_access channel_data
  subscriber_analytics`: passed;
- Notebook JSON parsed and every code cell compiled;
- Markdown fence validation, staged credential-shaped-value scan, and
  `git diff --check`: passed;
- integration fixtures resolved genuine `workspace_access` sessions, contexts,
  and memberships rather than constructed contexts;
- concurrency fixtures proved one intent per idempotency key and exactly one
  provider exchange for four concurrent callbacks;
- colliding-identifier fixtures proved two workspaces with identical connection,
  intent, and credential slot identifiers never share reads, callbacks, cursors,
  credentials, cascades, or audit attribution;
- retention fixtures exercised each boundary one second before and exactly at
  the cutoff.

Five-axis review verdict: no unresolved Critical or Required finding.
Correctness covers intent binding, atomic claim, provider verification,
rotation, disconnect ordering, pagination, retention, and cascade boundaries.
Security covers digest-only state storage, PKCE S256, permission checks on every
public operation, non-enumerating errors, workspace-prefixed keys, redacted
secret rendering, revocation of every rejected grant, and fail-closed unknown
outcomes. Production functions are at most 65 lines. Two unused helpers were
deleted; the protocols and the audit sink are intentional seams, not dead code.

One implementation bug was found and fixed by its own regression test: the
authorization URL embeds the one-time state, so retaining an `AuthorizationStart`
in the 90-day idempotency record kept a live secret in domain state. The URL is
now an ephemeral secret deleted with its intent.

No Web route, HTTP adapter, real OAuth call, Google SDK, database, migration,
background job, UI, dependency, real credential, or real channel data was
introduced. The in-memory ephemeral store and credential vault are documented
fakes; an approved managed vault or KMS, key rotation, access auditing, and
backup erasure remain production gates.

### Next session

The next capability in the build order is `collection-jobs`, which depends on
`channel-connections` and `channel-data`. Before it starts, two contracts are
still unspecified and must be approved separately:

1. a workspace-bound execution authority that lets a background job act without
   a browser context, and
2. a brokered provider-operation interface, because no caller may request a raw
   credential from this module.

The hosted Web/API and non-engineer UI slice also remains unspecified; its
acceptance requirements are recorded in `SPEC-channel-connections.md`.

## Verified milestone: execution broker and collection-jobs complete

Branch: `feature/multi-channel-analytics`

Date: 2026-08-21

The user approved building the remaining capabilities, so the two contracts the
progress record listed as prerequisites were specified and implemented, followed
by the whole `collection-jobs` module.

### channel-connections additions

- `1db9697` specifies the execution authority and the brokered provider
  operation interface.
- `6fb1d72` implements them: a workspace-bound `ExecutionAuthority` that carries
  no credential and expires at exactly 60 minutes, revoked by disconnect,
  reported invalidation, and workspace deletion but not by credential rotation;
  and `run_provider_operation`, which resolves the credential slot internally,
  calls a `YouTubeDataGateway`, and returns only minimized subscriber, video, and
  comment-author rows with an opaque page token and the reported quota cost.
  An expired grant deletes the slot, publishes `REAUTH_REQUIRED`, revokes the
  connection's authorities, and fails closed. Two error codes were added:
  `AUTHORITY_NOT_FOUND_OR_EXPIRED` and `CONNECTION_REAUTH_REQUIRED`.

### collection-jobs

- `cbc24ca` approves `SPEC-collection-jobs.md` with the plan and six tasks.
- `3842279` adds the contracts and subscriber execution.
- `3fff6ce` adds quota, retries, and cancellation.
- `ffd2904` adds owner-content execution across both phases.
- `944fb88` adds schedules, reads, and bounded pagination.
- `19574fa` adds tenant isolation, retention, cascade, integration, and README.

Design decisions recorded here rather than re-derived later:

1. Run kinds are `SUBSCRIBERS` and `OWNER_CONTENT`. Owner content publishes the
   video inventory and finishes it before covering comments, because
   `channel-data` binds coverage to the accepted inventory identifier.
2. Each attempt uses its own `channel-data` collection id, so a retry never
   collides with the previous attempt's idempotency records.
3. `channel-data` accepts a failure code only on a `FAILED` finish, so a quota
   stop finishes `PARTIAL` with no code while the run itself records
   `QUOTA_EXHAUSTED`.
4. Quota affordability is checked before each call with a conservative
   reservation; the gateway's reported cost is then deducted.
5. Provider page size is a service setting, defaulting to 50.
6. Execution is synchronous and caller driven. No queue, worker, timer, or
   scheduler daemon exists in this slice.

Verification evidence:

- collection-jobs suite: 82 tests passed;
- channel-connections suite: 145 tests passed;
- channel-data suite: 35 tests passed;
- workspace-access suite: 40 tests passed;
- subscriber analytics suite: 29 tests passed;
- compile of all five packages, Notebook code cells, Markdown fences,
  `git diff --check`, and a staged credential-shaped-value scan passed;
- the end-to-end fixture drove provider rows through the broker into an accepted
  `SilentAnalysisDataset` that `analytics-core` classified into `NEW_SILENT`,
  `OLD_SILENT`, `DORMANT`, and `ACTIVE`;
- a real `workspace_access` session, workspace, and resolved contexts drove the
  connect-then-collect pipeline;
- production functions are at most 74 lines.

No dependency, network call, browser, database, queue, worker platform, real
credential, or real channel data was introduced.

### Next session

Remaining capabilities in build order are `analysis-api` and `web-ui`.

`analysis-api` can proceed the same way: a fixture-backed, standard-library
vertical slice over `channel-data` and `analytics-core` with saved views,
comparisons, and export contracts.

`web-ui` cannot start without one explicit decision, because the approved
specifications require asking first: which Web framework, HTTP/session stack,
and dependency set to adopt. The non-engineer UI acceptance requirements are
recorded in `SPEC-channel-connections.md`.

## Verified milestone: analysis-api and web-ui

Branch: `feature/multi-channel-analytics`

Date: 2026-08-21

Verified implementation HEAD: `f1cd207`

The user approved the recommended stack (FastAPI, Uvicorn, Jinja2 server-rendered
templates, no frontend build step) and approved building the remaining
capabilities, so both were implemented.

### analysis-api (`6e5fb82`)

`SPEC-analysis-api.md` plus a standard-library module over `channel-data` and
`analytics-core`:

- filtered analysis runs paged by single-use cursors bound to workspace, query,
  and the accepted snapshot/inventory generation;
- workspace-shared saved views with idempotent save and delete;
- a bounded multi-channel comparison that reports each channel's readiness
  reason instead of failing the whole request;
- a deterministic UTF-8 CSV export with BOM behind the separate
  `analysis.export` permission;
- every segment and filter rule delegated to `analytics-core`; the module adds
  no analytics logic of its own.

### web-ui (`f1cd207`)

First layer in this repository with third-party dependencies, pinned in
`web_ui/requirements.txt`: fastapi 0.141.1, uvicorn 0.52.4, jinja2 3.1.6,
python-multipart 0.0.32, httpx 0.28.1 (tests only). Domain modules remain
standard-library only.

- Guided Japanese flow: login, create workspace, connect channel, collect,
  analyse, export; one clear primary action per step.
- Session cookie is `HttpOnly`, `Secure`, `SameSite=Lax`, and revocable through
  `workspace-access`. Every POST requires a `SameSite=Strict` double-submit CSRF
  token. Every response carries CSP with `frame-ancestors 'none'`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and
  `Referrer-Policy: no-referrer`.
- Errors render a stable Japanese message plus the next action; no provider
  response, credential, or internal identifier reaches a page.
- The workspace cookie is only a hint: each request re-resolves a real
  `WorkspaceContext` and each module checks its own permission.
- Provider access is a synthetic in-process gateway (`web_ui/demo_provider.py`)
  and the consent screen states that it is a demo. No request leaves the
  process, no real credential exists, and no YouTube quota is consumed.
- `ChannelConnectionsService` now takes its authorization host allowlist as
  configuration so a deployment can point it at Google while the demo points at
  its own host. The default remains `accounts.google.com`.

### Verification evidence

- web-ui 15, analysis-api 19, collection-jobs 82, channel-connections 145,
  channel-data 35, workspace-access 40, subscriber analytics 29 — all passed;
- compile of all seven packages, Markdown fences, `git diff --check`, and a
  staged credential-shaped-value scan passed;
- web tests cover the whole flow end to end, CSRF rejection, unauthenticated
  redirect, security headers, readiness guidance before collection, filters,
  CSV download, disconnect-keeps-data, foreign workspace cookie isolation, and
  that no page renders a token, state, or vault slot;
- the application was started under real TLS with `uvicorn` and answered
  `GET /login` with 200 over HTTPS.

### Known verification gap

Interactive verification in a real Chrome window was **not** completed. The
local server needs TLS because the session cookie is `Secure`, and the browser
tooling cannot click through the self-signed-certificate interstitial. Options
for the next session, in order of preference:

1. install a locally trusted development certificate (for example `mkcert`) and
   repeat the guided flow in Chrome;
2. run behind a TLS-terminating proxy the machine already trusts;
3. keep automated coverage only, and treat browser usability and accessibility
   review as an explicit outstanding acceptance item.

Static page snapshots were rendered for layout review but the visual pass was
stopped before completion at the user's request.

### Remaining work

- Browser usability and accessibility verification (above).
- Real identity provider for login; the current login accepts a display name and
  is a demo placeholder.
- Real Google OAuth client registration, consent verification, and provider
  HTTP/SDK adapters to replace `web_ui/demo_provider.py`.
- Persistence, background workers for collection, managed credential vault or
  KMS, rate limiting, and deployment TLS.
- `CAPABILITY_MAP.md` now has all seven capabilities implemented as reference
  modules; nothing in the repository performs a real provider call or stores a
  real credential.

## Verified milestone: browser verification of web-ui

Branch: `feature/multi-channel-analytics`

Date: 2026-08-21

The known verification gap from the previous session is now closed: the guided
flow was operated end to end in a real Chrome window over TLS.

### How the certificate blocker was cleared

A self-signed certificate for `CN=localhost` (SAN `localhost`, `127.0.0.1`) was
generated outside the repository and passed to `uvicorn` with `--ssl-keyfile`
and `--ssl-certfile`. Chrome driven through `chrome-devtools-mcp` loads the page
despite reporting `ERR_CERT_AUTHORITY_INVALID`, so no trusted development
certificate had to be installed and no certificate store was modified.

The demo must be served on port 443. `channel-connections` rejects an
authorization URL that carries an explicit port, so `https://localhost:8443`
fails at "チャンネルを接続する" with `PROVIDER_AUTHORIZATION_FAILED`. The rule
is deliberate and was left alone; `web_ui/README.md` now documents the port.

### Defects the browser found that the suites did not

1. **Raw validation payload leaked (fixed).** Submitting the analysis filter
   with an empty "登録からの日数" produced FastAPI's raw 422 JSON, exposing the
   internal field name and parser type. No route had a `RequestValidationError`
   handler, so every route could leak one. A single handler now renders the
   standard Japanese `INVALID_INPUT` page, and the optional number query accepts
   an empty submission as "no filter" through `OptionalInt`. Note that FastAPI
   only applies `Annotated` metadata in the `Annotated[T, Query()]` form; the
   `param: T = Query(None)` form silently ignores the validator.
2. **Form controls rendered dark (fixed).** `:root` declared
   `color-scheme: light dark` while the palette is hard-coded light, so under a
   dark OS theme every input, select, and checkbox rendered dark on a light
   page. The declaration now matches the palette.
3. **Checkbox stretched to 12rem (fixed).** `input, select { min-width: 12rem }`
   also hit the checkbox, pushing its label far to the right. The rule now
   excludes checkboxes.
4. **Run history showed raw enums (fixed).** "最近の収集" rendered
   `OWNER_CONTENT`, `SUBSCRIBERS`, and `SUCCEEDED`, breaking the non-engineer
   requirement that the only technical term shown is the scope name. Added
   `RUN_KIND_LABELS` and `RUN_STATUS_LABELS` beside the existing label maps.

### Known cosmetic item, deliberately not fixed

The analysis page still prints `対象チャンネル: UC_demo_channel` rather than the
channel title. `analysis-api` carries no title, and reading one would require
the analysis route to also resolve `channel.read`, coupling analysis rendering
to a second permission for a cosmetic gain. Revisit if a title is added to
`AnalysisSummary`.

### Verification evidence

- Guided flow operated in Chrome: login, create workspace, connect channel
  through the demo consent screen, collect, analyse, filter, export;
- the empty-filter and invalid-value cases were re-checked in the browser after
  the fix: 200 with three filtered rows, and the Japanese `INVALID_INPUT` page
  with no internal field name;
- CSV export fetched in-page: 200, `attachment`, `text/csv;charset=utf-8`, and
  the expected header row;
- console clean: no errors or warnings across the whole flow;
- mobile viewport 390x844: no horizontal scrolling, every control reachable;
- accessibility observed in the a11y tree: labelled controls, `scope`-carrying
  table headers, and a polite live region for each status message;
- suites: web-ui 18, analysis-api 19, collection-jobs 82, channel-connections
  145, channel-data 35, workspace-access 40, subscriber analytics 29 — all
  passed;
- `compileall` over the seven packages and `git diff --check` passed.

No dependency, network call, real credential, or real channel data was
introduced. The certificate lives outside the repository.

### Remaining work

Unchanged from the previous session apart from the closed browser gap: a real
identity provider, real Google OAuth client registration and provider adapters,
persistence, background workers, a managed credential vault, rate limiting, and
deployment TLS.
