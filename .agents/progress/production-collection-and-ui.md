# Production collection recovery and UI publication

Date: 2026-08-23

## Objective

Reproduce the reported post-deployment collection failure with the real hosted
account, identify its recorded cause without exposing credentials, improve the
recovery/history UI, and publish the existing internal application capabilities.

## Verified production evidence

- The signed-in hosted account had four failed runs, all recorded as
  `REAUTH_REQUIRED` with zero provider quota spent.
- The failures occurred immediately after a cold process start forced the
  persisted refresh credential to be exercised again.
- Secret Manager version metadata and the safe provider log showed the rejected
  grant and subsequent reauthorization without reading or printing any token.
- After reauthorization, a real-browser collection completed for subscribers
  and owner content, spending three provider quota units in total.
- Cloud Run routes 100% to `yna-web-00014-g2t` with a maximum of one instance;
  a multi-instance cache race was ruled out for this incident.

## Completed local milestone

- Google channel authorization now requests `prompt=consent select_account` so
  a multi-account operator explicitly chooses the channel-owning account.
- Recent collections now render a JST execution timestamp, channel title,
  result, safe failure detail, and quota usage.
- RED/GREEN tests cover account selection, timestamps, and actionable
  `REAUTH_REQUIRED` history.
- Verification command:
  `python -m unittest web_ui.tests.test_google_provider web_ui.tests.test_web -q`
  — 132 passed, one existing Windows-only POSIX test skipped.

Current branch: `feature/multi-channel-analytics`.

## Pending

1. Publish schedules, multi-channel comparison, saved views, and membership
   capabilities through permission-aware web routes.
2. Run full Windows, Linux/POSIX, Docker, and real-browser verification.
3. Review, commit remaining slices, and push the branch.

