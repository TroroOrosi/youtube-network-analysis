# Task List: analytics-core

Status: approved
Plan: [`tasks/plan.md`](plan.md)
Spec: [`SPEC-analytics-core.md`](../SPEC-analytics-core.md)

Complete tasks in order. Every behavior task uses red-green-refactor: add the
focused failing test, confirm the expected failure, implement the smallest
change, then run focused and full verification before committing.

## Task 1: Deliver default analysis with silent totals

**Description:** Create the immutable `analytics-core` contract and the smallest
complete `analyze` path using a fixed fixture that covers all four segments.
This first slice must already answer the core silent-subscriber question.

**Acceptance criteria:**

- [x] Immutable records, stable enums, `AnalysisValidationError`, and the public
  `analyze` entry point match the approved specification.
- [x] A fixed reference time and fixture produce `NEW_SILENT`, `OLD_SILENT`,
  `DORMANT`, and `ACTIVE`, plus correct scope/filtered silent totals.
- [x] Results include the public-subscriptions limitation, do no I/O, and do not
  mutate caller-owned inputs.

**Verification:**

- [x] RED then GREEN: `python -m unittest subscriber_analytics.tests.test_analytics_core -v`
- [x] Regression: `python -m unittest discover -s subscriber_analytics/tests -v`
- [x] Compile: `python -m compileall -q subscriber_analytics`
- [x] Integrity: `git diff --check`

**Dependencies:** None

**Files likely touched:**

- `subscriber_analytics/analytics_core.py`
- `subscriber_analytics/tests/test_analytics_core.py`

**Estimated scope:** Small (2 files)

## Task 2: Complete filters, validation, and determinism

**Description:** Finish the core contract by adding every approved filter,
cutoff boundary, validation error, and deterministic ordering rule.

**Acceptance criteria:**

- [x] Subscription windows/dates, never-commented, no-comment-within, latest
  observation, and segment filters use inclusive boundaries and AND semantics.
- [x] Duplicate IDs, invalid activity/counts/ranges, naive times, and invalid
  windows raise the specified stable error codes without partial results.
- [x] Permuting equivalent input yields identical ordered rows and summaries;
  non-subscriber comment activity is safely ignored.

**Verification:**

- [x] RED then GREEN: `python -m unittest subscriber_analytics.tests.test_analytics_core -v`
- [x] Regression: `python -m unittest discover -s subscriber_analytics/tests -v`
- [x] Compile: `python -m compileall -q subscriber_analytics`
- [x] Integrity: `git diff --check`

**Dependencies:** Task 1

**Files likely touched:**

- `subscriber_analytics/analytics_core.py`
- `subscriber_analytics/tests/test_analytics_core.py`

**Estimated scope:** Small (2 files)

## Checkpoint A: Core contract

- [x] Tasks 1-2 acceptance criteria are all checked with evidence.
- [x] Focused and full tests pass from a clean command invocation.
- [x] Code-review graph is rebuilt and shows the new module without parse errors.
- [x] Contract/core commits are pushed before adapter migration starts.

## Task 3: Migrate the CLI and preserve silent-analysis output

**Description:** Turn `extract_silent.py` into a file/CLI adapter over
`analytics_core.analyze`, retaining its safety gates and public behavior.

**Acceptance criteria:**

- [x] Existing flags, `OUTPUT_COLUMNS`, Japanese segment labels, inclusive date
  behavior, and the public-subscription caveat remain compatible.
- [x] Comment collection coverage is validated before core invocation, so
  incomplete or API-key-only comments still fail closed by default.
- [x] A temporary fixture-backed CLI regression produces correct never-commented,
  no-comment-within, and new/old silent results without network or credentials.

**Verification:**

- [x] RED then GREEN focused adapter tests in `test_analytics.py`.
- [x] Core: `python -m unittest subscriber_analytics.tests.test_analytics_core -v`
- [x] Full: `python -m unittest discover -s subscriber_analytics/tests -v`
- [x] Runtime: execute `extract_silent.main()` against a temporary fixture and
  assert the generated ordered CSV.
- [x] Compile and integrity checks pass.

**Dependencies:** Tasks 1-2, Checkpoint A

**Files likely touched:**

- `subscriber_analytics/extract_silent.py`
- `subscriber_analytics/tests/test_analytics.py`
- `subscriber_analytics/tests/test_analytics_core.py`

**Estimated scope:** Medium (3 files)

## Task 4: Migrate the Notebook to the shared core

**Description:** Replace the Notebook's direct DataFrame calculation calls with
the same typed analysis entry point used by the CLI.

**Acceptance criteria:**

- [x] Existing Notebook controls and Japanese display/output remain available,
  including silent-period and never-commented selection.
- [x] Notebook code calls `analytics_core.analyze` and contains no independent
  segment or filter calculation.
- [x] Notebook JSON parses and every code cell compiles after the edit.

**Verification:**

- [x] Notebook JSON/code-cell compile command from `tasks/plan.md` passes.
- [x] Full: `python -m unittest discover -s subscriber_analytics/tests -v`
- [x] Compile: `python -m compileall -q subscriber_analytics`
- [x] Integrity: `git diff --check`

**Dependencies:** Task 3

**Files likely touched:**

- `subscriber_analytics/subscriber_analytics.ipynb`
- `subscriber_analytics/tests/test_analytics.py`

**Estimated scope:** Small (2 files)

## Checkpoint B: Adapter integration

- [x] Tasks 3-4 acceptance criteria are all checked with evidence.
- [x] CLI fixture runtime and Notebook syntax verification pass.
- [x] Graph callers show CLI and Notebook both using `analytics_core.analyze`.
- [x] Adapter commits are pushed before cleanup starts.

## Task 5: Remove superseded calculation paths

**Description:** After graph verification proves the old calculation helpers are
unreferenced, remove only those superseded paths.

**Acceptance criteria:**

- [x] Graph queries confirm `build_table`, `add_segments`, `apply_filters`, and
  `format_output` have no remaining consumers before any deletion.
- [x] CLI and Notebook continue to use `analytics_core.analyze` after the old
  helpers are removed.
- [x] No collector, OAuth, dependency, CLI flag, CSV column, or unrelated
  analysis behavior changes in this cleanup.

**Verification:**

- [x] Core and full test suites pass.
- [x] Compile and Notebook code-cell checks pass.
- [x] `git diff --check` and secret-value scan pass.
- [x] Code-review graph rebuild completes with no parser errors.

**Dependencies:** Tasks 3-4, Checkpoint B

**Files likely touched:**

- `subscriber_analytics/extract_silent.py`
- `subscriber_analytics/tests/test_analytics.py`

**Estimated scope:** Small (2 files)

## Task 6: Document the shared analysis behavior

**Description:** Update user-facing documentation after the code paths are
verified, describing one shared engine without turning the README into change
history.

**Acceptance criteria:**

- [x] README explains that CLI and Notebook use the same core calculation.
- [x] Silent definitions, comment-coverage requirements, filter behavior, and
  the public-subscriptions limitation remain explicit.
- [x] Commands and examples match the verified current interface exactly.

**Verification:**

- [x] Compare every documented command/flag with the current argparse surface.
- [x] Full test, compile, Notebook code-cell, and `git diff --check` commands pass.
- [x] Secret-value scan finds no credentials or credential-like values.

**Dependencies:** Task 5

**Files likely touched:**

- `subscriber_analytics/README.md`

**Estimated scope:** Extra small (1 file)

## Task 7: Review and checkpoint the module

**Description:** Perform the final graph-backed, five-axis review and preserve
continuation-critical evidence for the next hosted-app module.

**Acceptance criteria:**

- [x] Correctness, simplicity, architecture, security, and performance review
  has no unresolved Critical or Required findings.
- [x] Every specification success criterion and project Definition of Done item
  relevant to this module has verification evidence.
- [x] Progress record and task status identify exact commits, verification
  outcomes, remaining risks, and the next module without credentials or data.

**Verification:**

- [x] Run graph `detect_changes`, `get_affected_flows`, and `tests_for` queries.
- [x] Run focused/core, full, compile, Notebook, integrity, and secret scans.
- [x] Confirm `git status --short --branch` contains only intended state.

**Dependencies:** Task 6

**Files likely touched:**

- `.agents/progress/youtube-analysis-app.md`
- `tasks/todo.md`
- Any source/test file required by a concrete review finding, capped at five
  files for this task; larger findings become a separately reviewed task.

**Estimated scope:** Medium (2-5 files)

## Final checkpoint: analytics-core complete

- [x] All seven tasks and both intermediate checkpoints are complete.
- [x] `analytics-core` is the sole segment/filter implementation used by CLI and
  Notebook.
- [x] Silent-subscriber results are covered by golden and adapter regressions.
- [x] No real OAuth flow, API collection, or production data was used.
- [x] Feature branch is pushed and ready for human review before merge.
