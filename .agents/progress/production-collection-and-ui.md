# Production collection recovery and UI publication

Date: 2026-08-23

## Objective and scope

Verify collection with the real hosted account without exposing credentials,
repair the reported collection and UI defects, review the application as a
whole, verify it in Windows, Linux/Docker, a real browser, and with POSIX file
permissions, then commit and push the scoped branch. Production deployment,
force-push, history rewriting, releases, and unrelated container changes are
explicit non-goals.

Repository: `C:\Users\pupu_\youtube-network-analysis`

Branch: `feature/multi-channel-analytics`

Current pre-checkpoint HEAD: `4c6b4f1`

Remote target: `origin/feature/multi-channel-analytics`

Review baseline: `4c66120`

## Verified production evidence

- The signed-in hosted account initially had four failed runs recorded as
  `REAUTH_REQUIRED`, with zero provider quota spent.
- The failures occurred when a restored refresh credential was exercised after
  a cold process start. No credential or token value was read or printed.
- After explicit reauthorization, a real-browser collection completed for
  videos/comments and subscribers. The UI reported completion and quota usage
  of two plus one units.
- The connection status then displayed as available.
- Production still routes 100% to revision `yna-web-00014-g2t`; its old UI does
  not yet contain the recent-collection date column. This task does not deploy.
- A later refresh reached the login page because the saved browser session had
  expired. No second collection and no additional provider quota were used.
- Console errors observed during browser verification came from browser
  extensions, not application JavaScript.

## Completed implementation

- Recent collection history renders a JST date/time, channel name, safe result
  detail, and provider quota use.
- Google authorization explicitly supports account selection and actionable
  reauthorization recovery.
- Schedules, comparisons, saved analysis conditions, and member management are
  permission-aware UI capabilities.
- The scheduler drain enqueues due recurring schedules before executing work.
- Due workspace discovery includes due schedules, not just queued runs.
- A schedule for a disconnected channel is removed lazily and cannot block
  other schedules or workspaces.
- Collection jobs use a narrow target resolver requiring only
  `collection.run`; its result contains only `connection_id` and
  `provider_channel_id`.
- Self-membership is labelled `退出する`; departure clears the selected
  workspace cookie, and multiple remaining workspaces render a selection page.
- Root analysis dependencies now include `google-auth-oauthlib`, which is
  imported by the OAuth implementation and was missing from a clean install.
- Specifications and module READMEs describe the current contracts and
  persistence documents.

## Verification evidence

- Windows unit/integration suites after the final workspace selector:
  - `analysis_api/tests`: 20 passed.
  - `channel_connections/tests`: 156 passed, 40 subtests passed.
  - `channel_data/tests`: 44 passed, 18 subtests passed.
  - `collection_jobs/tests`: 109 passed, 29 subtests passed.
  - `subscriber_analytics/tests`: 29 passed, 18 subtests passed in a clean
    `uv run --with-requirements requirements.txt --with pytest` environment.
  - `web_ui/tests`: 193 passed, 16 subtests passed, one Windows-only POSIX test
    skipped.
  - `workspace_access/tests`: 55 passed, four subtests passed.
  - Total: 606 passed; one expected platform skip.
- `ruff check` passed for all Python sources. The existing notebook alone has
  five E402 findings because it adds its directory before local imports.
- `python -m compileall -q` passed for all application packages.
- `pip check`: no broken requirements.
- `git diff --check`: passed; only Git's Windows LF-to-CRLF notices appeared.
- `docker build --check .`: passed.
- `docker build -t yna-web-review:current .`: passed after the final selector
  implementation.
- Linux Docker Web UI suite: 194 passed, including the POSIX-only test.
- Runtime image runs as `nobody` under Uvicorn; `/login` and
  `/assets/favicon.svg` returned 200. Test-only `pytest` and `httpx2` are absent.
- Persisted state directory mode is `0700`; all five state document files are
  `0600`.
- The task-owned verification container is `yna-web-review-http`, bound only to
  `127.0.0.1:18080`. The documented OAuth flow intentionally requires HTTPS on
  the default port, so local plain HTTP with an explicit port was not weakened.

## Review state

- Final Spec review passed with no missing, partial, incorrect, or scope-creep
  findings, including the clean-install OAuth dependency fix.
- Standards review passed. The existing 886-line `_register_routes` remains a
  judgement-call Long Function / Divergent Change; broad refactoring is outside
  this bug-fix checkpoint.
- `web_ui/templates/workspace_select.html` is staged with the rest of the scoped
  changes. The staged diff has been inspected and contains no high-risk secret
  values.

## Remaining steps

1. Commit atomically and push normally to the verified remote branch.
2. Verify local, upstream, and remote HEAD equality.
3. Remove only the task-owned verification container/image if cleanup is safe.
