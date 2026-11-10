# YouTube analysis experience renewal

## Scope and design contract

Request: improve `TroroOrosi/youtube-network-analysis`, inspect its deployed site
and Google Cloud, and renew how people use the application.

Branch: `feature/experience-renewal`, based on `origin/main` at
`594ac91be23a059e214214dc90435e37aefa0ab0`. Original working tree was clean.

Keep the existing FastAPI/Jinja/domain architecture and provider, authorization,
CSRF, quota, privacy, and persistence contracts. No dependency or Cloud service
is added. No migration, credential change, merge, release, or production write.

Design: retain the product's restrained palette, add a navy navigation rail and
teal actions, make channel selection/collection the home page's primary task,
explain the separate comment and public-subscription collection steps, keep
management secondary, label research/demo data, and provide one page-level
heading. Use a horizontal wrapping navigation on mobile, and local scrolling for
wide tables. No external font, script, image, or analytics service.

Acceptance: setup, channel actions, collection, filtering, saved views, comparison,
and report reading keep working; responsive layout at 320/768/1024/1440; no
unlabelled demo data; aliases enter Google login on the configured callback host.

## Verified observations (2026-11-10)

- `gcloud run services list --project=snappy-byway-498603-v9 --platform=managed`
  reported `yna-web`, `asia-northeast1`, Ready=True, revision
  `yna-web-p77f848f484-120709`. A filtered service description showed 100% traffic,
  max instances 1, concurrency 80, timeout 300 seconds.
- Configured application origin is
  `https://yna-web-893183842893.asia-northeast1.run.app`; service also advertises
  `https://yna-web-estujbebca-an.a.run.app`. The browser reached the latter's login
  page and account chooser; the callback uses the former. No account was selected.
- Scheduler `yna-drain` is ENABLED, cron `7 * * * *`. A read-only log query showed
  HTTP 200 responses at 05:07 and 06:07 UTC on 2026-11-10. Seven-day Cloud Run
  ERROR query returned no entries. These are bounded observations, not proof of
  successful owner-channel collection.
- Firestore `(default)` is native mode in `asia-northeast1`. No state documents,
  credential values, or YouTube data were downloaded.
- Existing progress records describe older deployments. Current evidence above
  takes precedence; do not reuse their rollout/rollback instructions blindly.

## Implemented and verified locally

- Login alias regression failed before the fix (`200 != 303`). Redirect now uses
  the configured origin before issuing the host-bound CSRF/OAuth state cookies.
  `python -X utf8 -m unittest web_ui.tests.test_web.GoogleLoginTests -q`:
  8 tests, OK. Existing tampered/replayed state tests retained.
- Successful sign-in clears the previous workspace cookie. The regression first
  reproduced an inaccessible workspace when switching identities, then passed
  with the shared session-response fix.
- Shared self-hosted CSS, navigation, home journey/channel actions, page headings,
  report interpretation, refresh actions and demo labels; subscriber filters
  precede saved views. No new dependency, collection driver or domain change.
- `python -X utf8 -m unittest web_ui.tests.test_experience web_ui.tests.test_web.GoogleLoginTests web_ui.tests.test_web.GuidedFlowTests web_ui.tests.test_multiday_ui -q`:
  46 tests, OK. New experience suite has five tests; existing authorization,
  tampered/replayed state, quota and collection assertions are preserved.
- `node --test web_ui/tests/collecting.test.cjs`: 6 passed, 0 failed after making
  the subprocess emit UTF-8 on Windows. Runtime collection driver is unchanged.
- `uv run --no-project --with-requirements web_ui/requirements-dev.txt --with-requirements subscriber_analytics/requirements.txt --with pytest --with openpyxl python -X utf8 -m pytest -q analysis_api/tests channel_connections/tests channel_data/tests collection_jobs/tests subscriber_analytics/tests web_ui/tests workspace_access/tests --tb=short`:
  **711 passed, 2 failed, 10 skipped, 203 subtests**, 53.75 seconds, Windows.
  Both failures are unchanged POSIX-only operations cases: release source chmod
  (Windows reports 0777 rather than 0755) and an owner-only backup guard that
  intentionally rejects non-POSIX. Do not weaken the guard to make Windows green.
  `git diff --exit-code origin/main -- web_ui/recover_owner_content.py web_ui/tests/test_production_release.py web_ui/tests/test_recovery_subscribers.py`:
  exit 0, no changes. Linux CI must verify these cases before release.
- Earlier fixture failures were fixed by extracting the collection form action
  instead of splitting the first `/connections/` string. No assertion was dropped.
- `ruff check web_ui/app.py web_ui/tests/test_web.py web_ui/tests/test_experience.py web_ui/tests/test_multiday_ui.py`:
  `All checks passed!`; `python -X utf8 -m compileall -q web_ui`: exit 0;
  `git diff --check`: exit 0.
- Real Chrome with synthetic fixtures: login/workspace setup, empty and collected
  homes, browser-driven collection, subscriber filter (OLD_SILENT + never
  commented: 3 filtered / 3 silent), network collection, live and reference
  reports. No page-level overflow at 320/768/1024/1440px; tables scroll locally.
  At 320px report metric content widths equal their 87px available widths, with
  22.4px type. Desktop evidence image is outside the repository at
  `C:/Users/pupu_/tmp/yna-experience-evidence/report-desktop.jpg`.
  Console messages were extension messaging errors; no application script error
  was observed. Viewport override was reset.
- Public production `/health/ready` and `/login`: HTTP 200, source
  `77f848f4846675e7283eee23b20524b53be23319`, revision above; readiness scope
  `application_initialization`, mode `google`. This does not probe dependencies.
  Read-only description confirms `FORWARDED_ALLOW_IPS=*` matches the container's
  proxy-header setup; alias redirect compares the reconstructed origin.
- Local HTTP cannot complete HTTPS-only provider authorization; no real Google
  account was selected. Owner OAuth and live YouTube collection remain untested.
- Docker Desktop was started, but `docker info` still could not find its Linux
  engine pipe. Existing Linux CI builds the actual non-root production image;
  local container startup is not claimed.
- Final source/impact review found no remaining required changes. Structural
  graph misses nested routes and partly parses HTML; exact source and tests were
  read for those areas. Build packaging already includes the new CSS file.

## Next steps

1. Commit/push the scoped change and open a reviewable PR under standing GitHub
   authorization. Verify the existing three-job Linux workflow and record its
   exact result in the PR. Do not merge.
2. Production rollout requires the existing single-writer maintenance/backup
   procedure. Obtain approval for the exact deployment after presenting the
   concrete tested change. No deployment has been performed.

Tools/skills: Agent Skills router, incremental implementation, frontend UI,
debugging, code review, git workflow; ponytail; progress-checkpoint. Code graph
generation initially `2026-11-10T06:57:36Z`; HTML has partial parse coverage and
static/scripts are excluded, so those paths were read directly. Context7 Jinja
source supports inheritance/autoescape; installed version is 3.1.6.
