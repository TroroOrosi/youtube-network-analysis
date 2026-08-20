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
