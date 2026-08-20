# Task List: collection-jobs

Status: approved
Plan: [`tasks/plan.md`](plan.md)
Spec: [`SPEC-collection-jobs.md`](../SPEC-collection-jobs.md)

Previous module: `channel-connections` (complete; see
[`.agents/progress/youtube-analysis-app.md`](../.agents/progress/youtube-analysis-app.md)).

Complete tasks in order with red-green-refactor: focused failing test, expected
failure, smallest complete behavior, then focused and regression verification
before committing.

## Task 1: Freeze run, schedule, and quota contracts

**Acceptance criteria:**

- [x] Immutable/slotted run, schedule, quota, page, and command values with
  stable enums and the nine safe error codes.
- [x] Boundary validation rejects unbounded identifiers, naive datetimes,
  non-positive quota, intervals under one hour, and out-of-range page limits.
- [x] Ports require `WorkspaceContext` on every tenant operation and depend
  only on the `channel-connections` broker and `channel-data` writer protocols.

**Verification:**

- [x] RED then GREEN: `python -m unittest collection_jobs.tests.test_models -v`
- [x] Existing suites, compile, and `git diff --check` pass.

## Task 2: Execute a subscriber run end to end

**Acceptance criteria:**

- [x] `enqueue_run` requires `collection.run`, rejects a second active run for
  the same connection and kind, and is idempotent per key.
- [x] `execute_run` issues a fresh execution authority, pages the broker,
  publishes one snapshot, finishes the `channel-data` collection `COMPLETE`,
  and reports pages and quota spent.
- [x] A `RUNNING` run cannot be executed twice and invalid transitions fail
  without mutation.

**Verification:**

- [x] RED then GREEN: `python -m unittest collection_jobs.tests.test_runs -v`
- [x] Full suites, compile, and integrity checks pass.

## Task 3: Enforce quota, retries, cancellation, and fail-closed reauth

**Acceptance criteria:**

- [x] A run stops before an unaffordable call, finishes `PARTIAL` with
  `QUOTA_EXHAUSTED`, and finishes its `channel-data` collection `PARTIAL`.
- [x] Transient failures requeue with exactly 1, 5, and 25 minute backoff and
  fail after three attempts; quota and reauth failures never auto-retry.
- [x] A revoked grant fails the run as `REAUTH_REQUIRED` and publishes nothing.
- [x] `cancel_run` stops a queued or running run and records `CANCELLED`.

**Verification:**

- [x] RED then GREEN: `python -m unittest collection_jobs.tests.test_policy -v`
- [x] Full suites, compile, and integrity checks pass.

## Task 4: Publish owner content in one accepted generation

**Acceptance criteria:**

- [x] An `OWNER_CONTENT` run publishes the video inventory, finishes it
  `COMPLETE`, then covers every video of that exact inventory, including videos
  with no comments.
- [x] Coverage failure before completion leaves the previous accepted dataset
  readable and promotes nothing.
- [x] Stored activity carries only per-video, per-author counts and latest
  times.

**Verification:**

- [x] RED then GREEN: `python -m unittest collection_jobs.tests.test_content -v`
- [x] Full suites, compile, and integrity checks pass.

## Task 5: Schedule, read, and paginate

**Acceptance criteria:**

- [x] Schedules require `collection.run`, enforce the one-hour minimum, and
  `enqueue_due_runs` creates at most one active run per connection and kind.
- [x] Run and schedule reads require `collection.read`, order newest first, and
  use opaque single-use cursors bound to workspace, query, and revision.
- [x] Missing and foreign runs or schedules share one safe error.

**Verification:**

- [x] RED then GREEN: `python -m unittest collection_jobs.tests.test_schedules -v`
- [x] Full suites, compile, and integrity checks pass.

## Task 6: Isolate tenants, enforce retention, integrate, and document

**Acceptance criteria:**

- [x] Identical run, schedule, and connection identifiers in two workspaces stay
  independent across execution, reads, cascade, and quota accounting.
- [x] Retention removes terminal runs, quota ledger entries, and idempotency
  records at exactly 90 days without purging unresolved runs.
- [x] An end-to-end fixture drives provider rows into a ready
  `SilentAnalysisDataset` that `analytics-core` classifies into all four
  segments, and the README documents usage, quota, retries, and limits.

**Verification:**

- [x] RED then GREEN across
  `collection_jobs.tests.test_tenant_isolation` and
  `collection_jobs.tests.test_integration`
- [x] Focused and full regression suites, compile, Notebook cells, Markdown
  fences, secret scan, graph review, and `git diff --check` pass.

## Final checkpoint: collection-jobs complete

- [x] All six tasks are committed and pushed.
- [x] Every success criterion has direct evidence.
- [x] No dependency, network call, credential, database, queue, or real data was
  introduced.
