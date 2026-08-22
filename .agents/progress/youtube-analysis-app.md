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

## Verified milestone: real Google provider adapters, rate limiting, deployment TLS

Date: 2026-08-21. Branch `feature/multi-channel-analytics`.

Three of the recorded remaining items are now built. They needed no external
resource and no new stack decision, so they were done before the four that do.

### Real provider adapters

`web_ui/google_provider.py` implements both provider ports against Google:

- `GoogleAuthorizationGateway`: an S256 PKCE, offline-access authorization URL
  on `accounts.google.com`; a code exchange that verifies the owner channel and
  probes `subscriptions?myRecentSubscribers=true` for the subscriber
  capability; provider revocation on disconnect.
- `GoogleDataGateway`: recent subscribers, the uploads playlist and its videos,
  and comment authors aggregated to a channel id, a count, and a latest time —
  no comment text, display name, or comment id ever leaves the adapter.
- `GoogleCredentialStore`: satisfies the write-only `CredentialVault` port and
  keeps the read side internal, so the gateways can refresh a token without any
  service or route being able to reach credential material. It is still the
  in-memory reference custody, not a KMS.

Transport is injected. The default is stdlib HTTPS with a 20-second timeout;
every test drives a fake transport, so the suite makes no network call, holds no
real credential, and consumes no quota.

Two constraints the code had to obey rather than work around:

1. `ProviderCredential.scopes` must equal the approved scope set exactly, so a
   grant carrying anything else is refused at the adapter with
   `SCOPE_NOT_GRANTED` instead of being stored.
2. Google returns the owner to the redirect URI with a **GET**, so `web_ui`
   gained `GET /oauth/callback`. It carries no CSRF token by construction; the
   module-verified `state` is the proof, and the session cookie is `SameSite=Lax`
   so it survives that top-level navigation.

`build_services(base_url, google=..., transport=...)` selects the real adapters,
and `google_config_from_env` requires both the client id and the secret — one
alone keeps the demo provider rather than sending an owner to a consent screen
that cannot complete.

### Rate limiting

Fixed-window counters in `web_ui`: 30 writes per minute per client and 3
collection runs per minute, refused with a Japanese 429 page. Reads are never
limited. Single process by design; a multi-instance deployment needs a shared
store, and this is not the provider quota guard, which `collection-jobs` owns.

### Content-Security-Policy fix

`form-action 'self'` would have blocked the redirect to Google after the
`接続する` form post in browsers that check the redirect chain. The directive now
allows exactly this origin and `https://accounts.google.com`.

### Deployment TLS

Documented in `web_ui/README.md`: terminate TLS at a proxy, run uvicorn on
loopback with `--proxy-headers`, require `X-Forwarded-Proto` and
`X-Forwarded-For` (without them the rate limiter sees only the proxy), and treat
uvicorn's own TLS flags as local review only.

### Verification evidence

- 44 new tests, each written before the code and watched fail: 29 for the
  adapters, 15 in `web_ui` for Google mode, configuration, rate limits, and the
  policy directive;
- Google mode is exercised end to end against a fake Google — start, consent
  return, connect, collect — proving the real adapter path without a network;
- suites: web-ui 60, analysis-api 19, collection-jobs 82, channel-connections
  145, channel-data 35, workspace-access 40, subscriber analytics 29 — all
  passed.

### Remaining work

Now genuinely blocked on a decision or an external action, not on code:

- **Google client registration and consent verification** — the deployment
  owner's action in Google Cloud. The adapters and the setup steps are ready.
- **Persistence** — under way; see the restart milestone at the end of this
  file. Three of the four services are done, `channel_connections` is not.
- **Background workers for collection** — needs persistence first, then a
  worker/queue platform decision.
- **A managed credential vault or KMS** — `GoogleCredentialStore` is the seam it
  would replace.


## Verified milestone: Google sign-in replaces the display-name login

`web_ui/google_login.py` runs the OpenID Connect authorization code flow with
S256 PKCE against the same registered client, asking for `openid profile email`
and never for a YouTube scope. It keeps no token after the exchange: the
userinfo response becomes a `VerifiedIdentity` and nothing else survives. The
pending state is single-use, expires in ten minutes, and lives in this process
only, so a restart cancels sign-ins in flight rather than honouring a stale one.

Two decisions worth keeping:

- **The demo door closes when a real one exists.** With a client configured,
  `POST /login` is refused with `LOGIN_METHOD_UNAVAILABLE`; leaving it open
  would let anyone claim an identity the provider is meanwhile verifying.
- **Every failure reads the same.** A refused consent, a replayed state and an
  unreachable Google all raise `LoginFailed` and render one message; the reason
  answers questions only an attacker asks.

### Verification evidence

- 21 new tests, each written before the code and watched fail: 13 for the flow
  itself (identity mapping, unverified email, PKCE verifier, replay, expiry,
  refusal, provider failure, incomplete response) and 8 in `web_ui` for the
  login page, the redirect to Google, the session it opens, the closed demo
  door, an invented state, a refused consent, the login callback registration,
  and the demo deployment that has no Google button at all;
- the Google-mode end-to-end flow now signs in through Google before connecting
  a channel, so the whole guided path runs on the real adapters against a fake
  Google;
- suites: web-ui 81, channel-connections 145, collection-jobs 82, workspace-
  access 40, channel-data 35, subscriber analytics 29, analysis-api 19 — all
  passed.

## Verified milestone: three of four services outlive a restart

Interrupted on request part-way through the fourth module. What is committed is
green; what is not committed is one deliberately failing test file, described
below.

Every stateful module now owns its own state format instead of sharing one, so
the modules stay independently extractable. The shape is the same in all three:

- a `StateStore` Protocol in `ports.py` — `load() -> str | None`,
  `save(document: str)`; the store keeps one text and knows nothing else;
- a `snapshot.py` that reads and writes that text;
- a `_StateLock` wrapping the service lock, which writes the document when the
  outermost hold ends: every command already mutates under that lock, so it is
  the one moment a snapshot is both consistent and impossible to forget;
- a version-guarded document. A document this code cannot read raises rather
  than starting empty — a deployment that silently forgot its data would report
  an empty channel as the truth and spend provider quota collecting it again.

Two decisions worth keeping:

- **`workspace_access` writes an explicit codec, the other two a tagged one.**
  Access state is small and its fields deserve to be listed by hand; sessions
  are written by digest only, never by secret. `channel_data` and
  `collection_jobs` hold deep nests of dataclasses, so their `snapshot.py`
  tags every value with its type and needs no field list to read one back.
- **Enums are encoded before primitives.** These enums subclass `str`; writing
  one as a bare string reads back as a string the model validators refuse.
  Discovered as a restart failure, fixed in the codec, and now covered.

### Verification evidence

- 31 new tests, each written before the code and watched fail:
  `workspace_access` 11, `channel_data` 9, `collection_jobs` 11;
- the quota test was re-checked against a service built without a store and
  failed there (`AssertionError: 3 != 0`), so it measures the ledger surviving
  and not the default budget;
- suites: workspace-access 51, channel-data 44, collection-jobs 91, web-ui 81,
  subscriber analytics 29, analysis-api 19 — all passed;
- channel-connections 155 ran with 10 errors, all of them the unfinished spec
  below.

### Interrupted here, and resumed

`channel_connections/tests/test_persistence.py` was left written and RED
(`TypeError: ChannelConnectionsService.__init__() got an unexpected keyword
argument 'state_store'`), uncommitted and untracked so the committed tree
stayed green. The next milestone below closes it.

## Verified milestone: the fourth service outlives a restart

`channel_connections` now has the same shape as the other three: a `StateStore`
Protocol in `ports.py`, a `snapshot.py` with the tagged codec, and a
`_StateLock` that writes the document when the outermost hold ends.

What is specific to this module:

- **The codec refuses credential material outright.** `AccessSecret`,
  `ProviderCredential`, and `VerifiedProviderGrant` are rejected in the first
  branch of `_encode`, before the dataclass branch, and `_type` refuses to name
  one on the way back in. `MemoryState` is secret-free by design today; the
  guard is there so a future field carrying a token fails loudly instead of
  leaking into a document.
- **A sign-in still underway is dropped.** `_persistable()` clears `intents`
  and `intents_by_state` before encoding. An intent can only be completed with
  its PKCE verifier, which lives in the ephemeral secret store and dies with
  the process; a restored intent would be an authorization that can never
  complete. Dropping it makes a late callback fail as the expired intent it
  really is. `_restore_start` already deletes a begin-idempotency record whose
  URL is gone, so a replayed `begin` after a restart starts a fresh sign-in
  instead of returning a dead intent id.
- **The attribute is `self._state_store`.** `self._store` collides with
  `collection_jobs`' private `_store(run)` method; that collision was a live
  bug there.

### Two assertions in the drafted test were wrong and were corrected

The test file was written from the module's spec before the module was read
back, and asserted two things that do not exist:

- `ConnectionStatus.DISCONNECTED`. The enum has only `ACTIVE` and
  `REAUTH_REQUIRED`, and SPEC-channel-connections requires disconnected
  metadata to be *removed* immediately, not tombstoned. Keeping the assertion
  would have contradicted the spec and
  `test_disconnect_revokes_deletes_and_hides_the_connection`. The test now
  asserts the connection does not come back after a restart, which is what it
  was for.
- `AUTHORIZATION_STATE_INVALID`. No such `ErrorCode`; an unknown state digest
  raises `INTENT_NOT_FOUND_OR_EXPIRED`.

### Verification evidence

- 10 new tests, watched fail first (all ten errored on the missing
  `state_store` keyword), now passing;
- channel-connections 155 passed, up from 155 with 10 errors;
- workspace-access 51, channel-data 44, collection-jobs 91, web-ui 81,
  analysis-api 19, subscriber analytics 29 — all passed. 470 in total;
- `python -m compileall -q channel_connections`: passed;
- `git diff --check`: passed;
- credential-literal scan of the changed files: no matches.

## Verified milestone: a deployment that actually survives a restart

Until now every module accepted a `StateStore` and nothing constructed one, so
no deployment kept anything. `web_ui/container.py` now builds one behind a
single environment variable.

- `YNA_STATE_DIR` unset keeps the previous behaviour exactly: the process
  starts clean and leaves nothing behind, which is what a demo run should do.
  Set, it holds one JSON document per module, so the modules stay independently
  extractable.
- `FileStateStore.save` writes a `0o600` temporary file and renames it over the
  previous document. A process killed mid-save leaves the old document intact
  rather than a truncated one, which the next start would refuse — correct, but
  a needless outage.
- The documents carry no credential. They do carry session digests and who may
  reach which workspace, hence the owner-only mode.
- **Credentials are still not kept.** The vault is in memory, so a restored
  connection is listed but must be authorized again before it can collect. This
  is the documented cost of not having the KMS yet, not an oversight.
- `ponytail:` the whole document is rewritten on every command, and two
  processes sharing one directory would let the last writer win. Both are
  written down in `web_ui/README.md`; they end when this becomes a database.

### What the tests found

- The dashboard never renders the workspace name when there is one workspace,
  so the restart test asserts the dashboard renders at all (rather than the
  "create your first workspace" page) and still lists the channel. That single
  page proves the session, the workspace, the membership and the connection all
  came back.
- Only the modules a sign-in and a connect actually touch are written.
  `channel_data.json` and `collection_jobs.json` do not exist after that flow,
  which is the per-module "nothing is written until something happens" rule
  working through the container.

### Verification evidence

- 6 new web-ui tests, watched fail first (`ImportError: cannot import name
  'state_dir_from_env'`), now passing;
- suites: channel-connections 155, workspace-access 51, channel-data 44,
  collection-jobs 91, web-ui 87, analysis-api 19, subscriber analytics 29 —
  476 in total, all passed;
- `python -c "import web_ui.main"` with `YNA_STATE_DIR` set: the real ASGI app
  builds, and the directory stays empty until a command runs;
- `python -m compileall -q web_ui channel_connections`, `git diff --check`, and
  a credential-literal scan of the changed files: all passed.

### The unobserved file mode, closed

The previous entry left `0o600` asserted only by a test that skips on Windows.
Closing it turned up a worse problem than the missing evidence.

- **The README was wrong, not just unverified.** It claimed files are "created
  readable by their owner only". On Windows `os.open`'s mode argument sets the
  read-only attribute and nothing else: the file inherits the directory's ACL.
  The claim is now split — POSIX enforces the modes, Windows does not and the
  host has to restrict the directory itself.
- **The directory was left at the default mode.** `mkdir(parents=True,
  exist_ok=True)` produces `0o755` under the usual `umask 022`, so the
  owner-only documents sat in a directory anyone could list. It now asks for
  `0o700`.
- **Two tests, because there are two claims.** `test_owner_only_modes_are_asked_for`
  spies on `os.open`/`os.mkdir` and asserts the modes this code *requests*; it
  runs everywhere and fails the day somebody drops the argument, which is the
  regression that matters. `test_the_documents_are_not_readable_by_other_accounts`
  asserts the host *enforces* them, and still skips where that is meaningless.
- **The state directory is no longer created by the test.** It was
  `tempfile.mkdtemp()`, which is already `0o700`, so the enforcement assertion
  would have passed without the code doing anything. The test now points at a
  path that does not exist and lets the application create it, which also
  covers a deployment naming a fresh directory.

### Observed on a real POSIX host

Run under `python:3.14-slim` (Python 3.14.7, `umask 0022`) against a tar of the
working tree — not a bind mount, which does not carry POSIX modes from Windows:

- all seven suites passed with no skips: channel-connections 155,
  workspace-access 51, channel-data 44, collection-jobs 91, web-ui 88,
  analysis-api 19, subscriber analytics 29 — 477 in total;
- driving the real ASGI app through sign-in and workspace creation reported
  `directory 0o700` and `workspace_access.json 0o600`. Under `umask 022` the
  default would have been `0o755`, so the explicit mode is what produced it.

Windows: the same 477 pass, with the one enforcement test skipped as designed.

`subscriber_analytics` needs `subscriber_analytics/requirements.txt` as well as
the root one; installing only the root file fails on `google_auth_oauthlib`.
That is an installation mistake, not a missing declaration.

### What a review of the persistence work found

Measured against the running demo app, not read off the graph — the graph
reported no affected flows and only its usual conservative private-helper test
gaps, and found none of this.

- **The write cost is per request, not per command.** Ten read-only dashboard
  views rewrote `workspace_access.json` eleven times: an authenticated page
  view touches the session's idle expiry, which is a state change like any
  other, and `_StateLock` writes whenever the outermost hold ends. Writing is
  correct — a session's expiry has to survive a restart — but the cost is a
  full rewrite of a document holding every user, session and audit event.
  `web_ui/README.md` said "on every command", which reads as writes only; it
  now says what actually happens.
- **A failed flush is reported as a failed command.** The command already
  succeeded in memory, so a caller treating `OSError` as "it did not happen" is
  wrong. Failing loudly beats losing durability silently, and the divergence
  self-heals: the next successful command writes the whole state. Verified,
  including that the lock stays usable afterwards.
- **A command that raises can still write the document.** The first failing
  command on a fresh service writes the empty-state document, because the
  initial `_document` is `None` and differs from `dump(MemoryState())`.
  Harmless — restoring an empty document equals no document — but "nothing is
  written until something happens" is weaker than its test implies.
- **`self._restored() or MemoryState()` is safe only because `MemoryState` has
  no `__bool__` or `__len__`.** Verified `bool(MemoryState()) is True`. Adding
  either would silently discard a restored state. All four modules share the
  idiom; `is None` would be the durable form.
- **The document names credential slots.** It holds
  `cred_intent_<high-entropy token>`, which is a vault key, not a credential —
  the README's "hold no credential" stays true. **The KMS must not treat
  knowing a slot id as authorization.**

Confirmed clean: no secret material in a real document (`state_digest` and
`verifier_slot_id` absent, so dropping intents works); values containing
newlines, tabs and Japanese round-trip byte-identical with no CR/LF written on
Windows; no `.writing` leftovers after a save.

### Next steps in order

1. A managed credential vault or KMS behind the existing `CredentialVault`
   port. Until then a restart lists every connection and can collect with
   none of them.
2. Background workers for collection, so a run outlives the request that
   started it.
3. The Google client registration, which only the deployment owner can do.

Command: `python -m unittest discover -s <module>/tests`, from the repository
root. `cd`-ing into the module first breaks the cross-module imports.

## Verified milestone: final repository, workflow, and UI review

Date: 2026-08-23

Branch: `feature/multi-channel-analytics`

Verified implementation HEAD: `4518b66d73acf31047c18e52253f64a52e15fa7e`

Reviewed the complete feature branch against `main`, including the approved
specifications, repository conventions, security boundaries, dependency and
container inputs, and the current web workflow and responsive/accessibility
surface.

Completed commits:

- `7be05a3` fixes filter-aware pagination and CSV export, adds a reauthorization
  path, moves collection progress from a side-effecting GET to a CSRF-protected
  POST, adds workspace creation/selection and empty-result recovery UI, improves
  keyboard/mobile table and action layouts, replaces duplicate segment cards
  with an accessible distribution meter, and tightens response headers.
- `0c16e87` changes the Docker build context to an allowlist, excludes local
  analysis artifacts and secrets, and moves test-only `httpx` to the development
  requirements.
- `4518b66` puts the bounded collection step under the same 3-request-per-minute
  rate limit as collection enqueueing, centralizes web filter conversion and
  query serialization, and replaces a private test-state mutation with an
  explicit demo dataset injection seam.

Verification evidence:

- workspace-access: 54 tests passed;
- channel-connections: 155 tests passed;
- channel-data: 44 tests passed;
- collection-jobs: 108 tests passed;
- analysis-api: 19 tests passed;
- web-ui: 183 tests passed;
- subscriber-analytics: 29 tests passed;
- total: 592 tests passed, with one expected Windows-only POSIX permission
  enforcement test skipped;
- `python -m compileall`: passed;
- `ruff check web_ui/app.py web_ui/container.py web_ui/tests/test_web.py`: passed;
- `git diff --check`: passed;
- `python -m pip check`: passed;
- `pip install --dry-run -r web_ui/requirements-dev.txt`: passed;
- no tracked `.env`, PEM/private-key file, or OAuth client secret was found.

The final Spec review found no missing, partial, unrequested, or incorrect
behavior in the review diff. The final Standards review found no hard violation
or high-confidence smell. Its earlier findings on collection-step rate limiting,
filter propagation, and private test state are all closed by `4518b66`.

Verification limitations:

- no browser instance was available in this session, so screenshots and
  interactive real-browser validation were not run;
- Docker CLI was installed but Docker Desktop's daemon was stopped, so the
  image build itself was not run.

Current product boundary: the web UI exposes sign-in, workspace creation and
selection, channel connection/reauthorization, collection progress, single-
channel filtered analysis, pagination, and CSV export. Membership management,
scheduled collection, multi-channel comparison, and saved views exist in the
domain/application capabilities but are not exposed in the approved web UI
slice.

Next steps:

1. Commit this progress record as a narrow checkpoint.
2. Push `feature/multi-channel-analytics` to its matching `origin` branch and
   verify local and remote HEAD equality.
