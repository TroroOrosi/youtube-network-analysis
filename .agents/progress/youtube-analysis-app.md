# YouTube analysis app progress

Last updated: 2026-08-20

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

## Current next steps

1. Review `SPEC-workspace-access.md`, currently proposed for review. It defines
   identity/session separation, single-workspace contexts, the `OWNER`/`MEMBER`
   permission matrix, tenant-selection failure behavior, session/CSRF rules,
   revocation, non-enumerating errors, and privacy/retention obligations.
2. Do not write an implementation plan or introduce persistence, Web OAuth,
   authentication providers, HTTP routes, or UI until that specification is
   approved.
3. After approval, plan and implement `workspace-access` with fixtures and
   in-memory fakes only. Continue to `channel-data` after the isolation boundary
   is verified, then threat-model `channel-connections` before hosted OAuth.

No database, dependency, endpoint, authentication flow, credential, real user
data, or production behavior was introduced while proposing the specification.

Recommended next-phase skills: `spec-driven-development`,
`security-and-hardening`, and `api-and-interface-design`. Use the code-review
graph before codebase exploration.
