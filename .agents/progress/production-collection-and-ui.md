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

Deployed code commit: `896e2d6`

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
- The authenticated hosted UI now publishes the aggregate-only audience
  network report and a matching nine-sheet Excel workbook. All seven analysis
  families are labelled as anonymized demo/research data with retrieval date.
- Generated JSON and XLSX are validated together at startup; missing,
  malformed, or schema-drifted artifacts fail closed. Formula-leading Excel
  text is inert, empty analysis families render clear states, and no raw IDs,
  edges, credentials, tokens, or local paths are published.
- The ordinary per-channel analysis page no longer repeats the provider's raw
  channel ID as visible UI. Hidden IDs required for filters, views, and exports
  remain unchanged.

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
- Audience-report closure reviews reported zero unresolved Spec or Standards
  findings. The final full suite passed with 622 tests, one expected Windows
  POSIX skip, and 132 subtests.
- The audience workbook has nine sheets, three populated charts, no formulas
  or formula errors, deterministic content, and the same headline metrics as
  the Web report. Its SHA-256 is
  `FEFCC9D3D8D2234B7CC80BA533C75D2725D58F9DDC2243822B58B6CB3597D267`.
- The final Docker image starts as `nobody`; aggregate JSON/XLSX startup
  validation passes, and a Linux state directory/file check confirmed `0700`
  and `0600` modes. Both dependency audits found no known vulnerabilities.

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

The previous known-good revision is `yna-web-00019-rok`. If the new revision
shows a data-integrity issue, new application error, or material latency
regression, route traffic back with:

```powershell
gcloud run services update-traffic yna-web --region asia-northeast1 `
  --to-revisions yna-web-00019-rok=100
```

No database migration or destructive state transformation was part of this
release, so rollback requires only a traffic change.

## Audience-network production publication

- GitHub commits: `302dc96` (report UI/XLSX), `a5fd2ad` (artifact and Excel
  hardening), `3da29d7` (Cloud Build allowlist), and `896e2d6` (remove the
  redundant visible provider ID).
- Cloud Run revision `yna-web-00021-mil` is Ready and receives 100% of traffic.
  It was deployed with zero traffic first, checked through the tagged candidate
  URL, and then promoted.
- The first candidate correctly failed closed because `.gcloudignore` excluded
  all XLSX files. The boundary now still excludes every CSV and generic XLSX,
  while allowlisting only
  `web_ui/assets/audience-network-analysis.xlsx`.
- Authenticated production browser verification on
  `https://yna-web-893183842893.asia-northeast1.run.app` confirmed the signed-in
  account's completed 15:48 JST collection history, the zero-public-subscriber
  live analysis, all seven demo analysis sections, retrieval date 2026-06-22,
  six headline metrics, responsive 320/768/1440 layouts, and a successful Excel
  download.
- Final revision access logs show HTTP 200 for `/audience-network` and
  `/audience-network/report.xlsx`; the ERROR and HTTP 5xx queries returned no
  entries.
- Immediate rollback target: `yna-web-00019-rok`, which was the previously
  verified audience-report revision. Conservative pre-feature rollback target:
  `yna-web-cd2b7c1`; it remains Ready.

## Current state

- Production traffic: 100% `yna-web-00021-mil`.
- Deployed application code is saved in GitHub at `896e2d6`.
- The prior durable progress checkpoint is saved in GitHub at `704e765`.
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
  non-empty results. These network/community analyses are now published in the
  authenticated hosted UI as clearly labelled anonymized demo/research data.
- Cloud Run now routes 100% to `yna-web-00021-mil`; the final revision has no
  ERROR-level or HTTP 5xx entries.

## GitHub progress checkpoint

Recorded: 2026-08-23 (Asia/Tokyo)

- Repository: `TroroOrosi/youtube-network-analysis`.
- Branch: `feature/multi-channel-analytics`.
- Checkpoint baseline: `704e765` (`docs(progress): record audience report
  production rollout`).
- Before this checkpoint, the worktree was clean and the local branch matched
  `origin/feature/multi-channel-analytics`; an explicit push returned
  `Everything up-to-date`.
- The production application remains revision `yna-web-00021-mil` with 100%
  traffic at
  `https://yna-web-893183842893.asia-northeast1.run.app`.
- The completed scope, authenticated collection evidence, hosted analysis and
  Excel verification, test totals, deployment state, and rollback targets are
  recorded above. No credential, token, raw account identifier, or personal
  email address is included in this record.
- There is no known implementation, deployment, collection, analysis, Excel,
  browser, Docker, or POSIX-verification task remaining from this workstream.
