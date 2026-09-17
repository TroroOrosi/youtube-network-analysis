# Multi-day collection and subscriber traversal

## Scope / design

Extend PR #10 without deploying or modifying production data. Provider-imposed result
limits cannot be removed by this application. New subscriber traversals use the
ordinary `mySubscribers` feed and follow every returned continuation, with no
1,000-row/20-page stop. Preserve older `myRecentSubscribers` cursors without mixing
feeds. Keep public-subscription/provider-limit coverage warnings.

Daily exhaustion must suspend all three traversal phases, retaining the exact
cursor, deduplicated rows, run ID, attempt, usage and accepted data. Distinguish the
application's existing UTC workspace budget from YouTube daily errors resetting at
America/Los_Angeles midnight (including DST). Do not reset the old ledger. Do not
rotate API keys/projects or weaken OAuth/tenant boundaries. Quota waiting alone must
not become a failure after three days. Completion still requires traversal exhaustion.

## Implementation / verification sequence

1. Add regression tests exercising >1,000 subscriber rows, daily exhaustion during
   subscriber/video traversal, durable restarts, preserved accepted data and quota.
   Run them on the baseline and record expected failures.
2. Add typed provider-daily-quota propagation without invalidating credentials; keep
   denied-request cost accounting and preserve the failed page. Test both OAuth
   and public-comment adapters and ordinary 403 errors separately.
3. Implement UTC local-budget waits and PT provider waits using the current queue /
   checkpoints. Test DST, no early provider calls, no same-day repeat and cancel.
4. Switch new subscriber traversals to mySubscribers with a versioned all-feed
   cursor. Existing unprefixed recent-feed cursors continue their original query.
5. Expose waiting reason and next eligible time; clarify provider-returned coverage.
   Extend the existing operator recovery with an explicit opt-in fresh subscriber
   run, preserving the original successful run and all channel_data.
6. Run complete available Python / JavaScript suites, compile and critical lint.
   Publish to existing PR #10 and verify CI with repository-pinned dependencies.
   Remove the temporary read-only runtime export workflow. Update operator docs.

## Operational acceptance

Tests use synthetic data, not uploaded user records. No GitHub merge, Cloud Run
change, YouTube collection or billing operation in this task. The operator must
back up state, stop all writers, deploy the updated runtime, then use the recovery
PLAN/APPLY workflow. A queued record is not proof of execution or analysis completion.
