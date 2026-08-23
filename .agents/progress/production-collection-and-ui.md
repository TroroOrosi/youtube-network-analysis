# Production collection recovery and UI publication

Date: 2026-08-23

## Objective and scope

Verify the reported production collection failure with the real hosted account
without exposing credentials, repair collection and UI defects, review the
application as a whole, verify Windows, Linux/Docker, browser, and POSIX
behavior, save the work to GitHub, and deploy the reviewed revision to Cloud
Run. Force-push, history rewriting, releases, credential changes, and unrelated
Docker resources remain out of scope.

Repository: `C:\Users\pupu_\youtube-network-analysis`

Branch: `feature/multi-channel-analytics`

Review baseline: `4c66120`

Deployed code commit: `cd2b7c190b4a029371cd9fe7421d8268683aa147`

Remote target: `origin/feature/multi-channel-analytics`

## Verified collection recovery

- The signed-in hosted account initially had four failed runs recorded as
  `REAUTH_REQUIRED`, with zero provider quota spent.
- The failures occurred when a restored refresh credential was exercised after
  a cold process start. No credential or token value was read or printed.
- After explicit reauthorization, a real-browser collection completed for
  videos/comments and subscribers. The UI reported completion and quota usage
  of two plus one units, and the connection displayed as available.
- No second collection was started during deployment verification, so no
  additional provider quota was consumed.

## Completed implementation

- Recent collection history renders a JST date/time, channel name, safe result
  detail, and provider quota use.
- Google authorization supports explicit account selection and actionable
  reauthorization recovery.
- Schedules, comparisons, saved analysis conditions, and member management are
  permission-aware UI capabilities.
- The scheduler drain enqueues due recurring schedules before executing work,
  and due workspace discovery includes schedules as well as queued runs.
- A schedule for a disconnected channel is removed lazily and cannot block
  other schedules or workspaces.
- Collection jobs use a narrow target resolver requiring only
  `collection.run`; its result contains only `connection_id` and
  `provider_channel_id`.
- Self-membership is labelled `退出する`; departure clears the selected
  workspace cookie, and multiple remaining workspaces render a selection page.
- Root analysis dependencies include the OAuth flow package required by clean
  installs.
- A comparison row whose data is not ready now shows a concrete link to connect
  or collect the channel.
- Repeated workspace selection and creation forms share one template include,
  preserving CSRF, labels, required state, current selection, and input limits.

## Review and verification

- Final Spec review found one low-severity missing next action on an incomplete
  comparison. It was fixed and the closure review reported zero findings.
- Final Standards review found no hard violations. Its duplicated-template
  judgement call was fixed and the closure review reported no new smell.
- The existing 886-line `_register_routes` remains a judgement-call Long
  Function / Divergent Change. Broad router separation is intentionally kept
  out of this production bug-fix checkpoint.
- Fresh Windows suites after the review fix:
  - `analysis_api/tests`: 20 passed.
  - `channel_connections/tests`: 156 passed, 40 subtests passed.
  - `channel_data/tests`: 44 passed, 18 subtests passed.
  - `collection_jobs/tests`: 109 passed, 29 subtests passed.
  - `subscriber_analytics/tests`: 29 passed, 18 subtests passed.
  - `web_ui/tests`: 193 passed, 16 subtests passed, one Windows-only POSIX test
    skipped.
  - `workspace_access/tests`: 55 passed, four subtests passed.
  - Total: 606 passed; one expected platform skip.
- Linux Docker Web UI suite: 194 passed, including the POSIX-only test.
- `ruff check`, `compileall`, `git diff --check`, Dockerfile validation,
  and the final Docker image build passed.
- The production dependency audit reported no known vulnerabilities.
- The runtime image runs as `nobody`; persisted state directory mode is
  `0700` and all five local state document files are `0600`.
- The staged review fix contained no high-risk secret values.

## Production deployment

- Cloud Run service: `yna-web`, project `snappy-byway-498603-v9`, region
  `asia-northeast1`.
- New revision: `yna-web-cd2b7c1`.
- Image digest:
  `sha256:136836d41dae82920491cbb1a8521a233a4b06815e22d6717e4337456288007f`.
- The revision was deployed with no traffic and checked through the temporary
  `review-cd2b7c1` URL before rollout.
- Canary results: `/login` 200, favicon 200, unauthenticated drain 401,
  container Ready/Healthy, and no ERROR log entries.
- Traffic then moved to `yna-web-cd2b7c1` at 100%; the temporary traffic tag
  was removed.
- Canonical production results: home 303 to login, login 200, favicon 200,
  required security headers present, no HTTP 500 entries, and no ERROR logs.
- Scheduler `yna-drain` remains ENABLED and targets the canonical
  `/internal/drain` URL with the same audience.
- Service account, environment variable names, maximum one instance,
  concurrency 80, and timeout 300 seconds match the previous revision.
- Real-browser rendering reached the production login page and the Google
  account chooser. No account was guessed or selected during post-deploy
  verification; the earlier signed-in collection evidence remains the
  authenticated critical-flow proof.

## Rollback

The previous known-good revision is `yna-web-00014-g2t`. If the new revision
shows a data-integrity issue, new application error, or material latency
regression, route traffic back with:

```powershell
gcloud run services update-traffic yna-web --region asia-northeast1 `
  --to-revisions yna-web-00014-g2t=100
```

No database migration or destructive state transformation was part of this
release, so rollback requires only a traffic change.

## Current state

- Production traffic: 100% `yna-web-cd2b7c1`.
- Deployed code is saved in GitHub at `cd2b7c1`.
- No known blocker remains.

## Post-rollout authenticated analysis verification

- A further authenticated collection completed on 2026-08-23 at 15:48 JST:
  videos/comments completed with provider usage 2, and subscribers completed
  with provider usage 1. The production dashboard shows the execution date,
  channel, result, detail, and usage columns.
- The connected production channel has zero publicly visible subscribers, so
  its analysis scope, filtered result, silent total, and all four segment
  totals are correctly zero. The page explicitly explains that private
  subscriptions cannot be obtained from YouTube.
- The production comparison page lists the one connected channel, explains
  that at least two collected channels are required, and disables comparison.
- Demo-backed hosted analysis verification passed independently:
  - Web analysis flow: 9 passed (collection-to-analysis, empty/pre-collection
    guidance, filters, pagination, CSV, saved conditions, and comparison).
  - Analysis API: 20 passed.
  - Analytics core: 14 passed and 12 subtests passed.
- The repository's offline audience-network data also recomputed successfully:
  808 viewers, 106,568 subscribed channels, 235,024 viewer-channel edges,
  14,694 projected network nodes, 226,516 projected edges, and 50 communities.
  Top co-subscriptions, viewer breadth, category distribution, community size,
  affinity lift, and popularity-versus-affinity calculations all returned
  non-empty results. These network/community analyses are not currently
  exposed by the hosted Web UI.
- Cloud Run still routes 100% to `yna-web-cd2b7c1`; a fresh two-hour query found
  no ERROR-level entries for the service.
