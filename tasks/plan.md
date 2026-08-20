# Implementation Plan: analytics-core

Status: approved
Date: 2026-08-20
Specification: [`SPEC-analytics-core.md`](../SPEC-analytics-core.md)
Capability: [`analytics-core`](../CAPABILITY_MAP.md)

## Overview

Extract the calculation path from `subscriber_analytics/extract_silent.py` into
a pure typed module, then migrate the CLI and Notebook to it without changing
their public controls or CSV format. The first complete slice must already
classify and total silent subscribers; later slices add all filters, strict
validation, and adapter compatibility.

This plan does not add a database, HTTP API, frontend, OAuth flow, background
worker, or dependency. Those belong to later capability modules.

## Architecture decisions

- `subscriber_analytics/analytics_core.py` owns the immutable input/output
  records, validation errors, segment enums, and the single `analyze` function.
- Public contracts contain standard-library values only. pandas remains behind
  the CLI/Notebook adapter boundary.
- Segment IDs are stable English machine values. Existing Japanese labels and
  CSV columns stay in `extract_silent.py` as presentation compatibility.
- Silent totals are computed by the core as `NEW_SILENT + OLD_SILENT`; adapters
  do not reimplement that rule.
- Core analysis trusts normalized records but validates their invariants at its
  public boundary. File parsing and comment-coverage checks remain adapter
  responsibilities.
- UTC conversion happens once at the core boundary. Naive datetimes and invalid
  ranges fail with typed errors.
- Tests use fixture records and a fixed reference time. No test contacts
  YouTube or reads credentials.

## Dependency graph

```text
Golden behavior fixture
        |
        v
Typed contract + validation
        |
        v
Default segmentation + silent totals
        |
        v
Filters + deterministic ordering
        |
        +-------------------+
        v                   v
CLI/file adapter       Notebook adapter
        |                   |
        +---------+---------+
                  v
          Compatibility review
```

The contract and default analysis are sequential foundations. CLI and Notebook
migrations depend on the full core contract; they may be implemented in either
order but are kept as separate checkpoints because Notebook JSON is a distinct
risk surface.

## Proposed implementation sequence

### Phase 1: Characterize and build the smallest complete core

1. Freeze a compact golden fixture covering all four segments, API subscription
   time precedence, latest-observation exclusion, and silent totals.
2. Add immutable contract records, stable enums, typed validation errors, and a
   failing-then-passing default `analyze` path.
3. Verify that default output includes new-silent, old-silent, dormant, active,
   total silent, deterministic rows, and the public-subscriptions limitation.

Checkpoint: focused core tests pass; existing subscriber tests still pass; the
module performs no I/O.

### Phase 2: Complete filters and boundary behavior

1. Add exact-cutoff tests and implementation for subscription and activity
   windows.
2. Add inclusive since/until dates, never-commented, no-comment-within, segment,
   and latest-observation filters with AND semantics.
3. Add duplicate, invalid-count, inconsistent-activity, naive-time, invalid
   range, input mutation, and permutation determinism tests.

Checkpoint: every specification error code and filter has a focused test; full
suite and compile check pass.

### Phase 3: Migrate the CLI adapter

1. Convert registry/comment DataFrames into core records after the existing
   file and comment-coverage validation steps.
2. Map core segment IDs back to current Japanese labels and preserve the exact
   `OUTPUT_COLUMNS`, CLI flags, inclusive date behavior, and output note.
3. Add a golden CLI adapter regression proving silent-subscriber output and
   segment summaries match the pre-extraction behavior.

Checkpoint: focused adapter test and full suite pass; a fixture-backed CLI run
produces the expected CSV without API or credential access.

### Phase 4: Migrate the Notebook adapter

1. Replace direct calls to `build_table`, `add_segments`, and `apply_filters`
   with the same core interface used by the CLI.
2. Keep all existing Notebook controls and Japanese presentation labels.
3. Parse the Notebook as JSON and compile every code cell in addition to the
   normal test suite.

Checkpoint: CLI and Notebook have one calculation implementation and no direct
segment/filter business logic remains outside `analytics_core.py`.

### Phase 5: Review and checkpoint

1. Rebuild the code-review graph and inspect impact, affected flows, callers,
   and test coverage.
2. Run the five-axis code review, remove only newly orphaned calculation code,
   and confirm no dependency or secret changes.
3. Update the durable progress record with verification evidence, commit atomic
   milestones, and push the feature branch.

Final checkpoint: all specification success criteria and the project Definition
of Done are satisfied; the branch is ready for human review before any merge.

## Verification commands

```powershell
# Focused core suite
python -m unittest subscriber_analytics.tests.test_analytics_core -v

# Full subscriber analytics suite
python -m unittest discover -s subscriber_analytics/tests -v

# Python compile check
python -m compileall -q subscriber_analytics

# Notebook JSON and code-cell syntax check
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"

# Patch integrity
git diff --check
```

For the CLI runtime checkpoint, use only a temporary fixture directory created
by the test. Do not use the ignored real `subscriber_analytics/data/` directory.

## Checkpoint policy

- Run focused tests after each red-green-refactor cycle.
- Run the full suite and compile check after every two implementation tasks and
  before every commit.
- Keep contract/core, CLI adapter, and Notebook adapter changes in separate
  commits when each verified slice is coherent.
- Do not push a broken checkpoint. A blocked progress record must label failures
  explicitly.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Golden expectations accidentally encode a new implementation instead of current behavior | High | Write expected fixture results before core implementation and compare the existing calculation path first |
| Stricter timezone/input validation rejects dirty historical CSV rows | Medium | Keep parsing/coercion in the file adapter; pass only normalized records to the core and test the failure message |
| CLI Japanese labels or column order change during enum migration | High | Keep explicit adapter mapping and assert the entire ordered column list plus label values |
| Silent users are falsely inferred from incomplete/public-only comments | High | Preserve `validate_comment_collection` before core invocation and retain the default fail-closed behavior |
| Latest-observation logic is mistaken for confirmed unsubscribe status | Medium | Use `is_in_latest_observation` terminology and preserve the user-facing caveat |
| Notebook duplicates logic after CLI migration | Medium | Make Notebook call `analyze`; graph-query remaining callers of old helpers before removal |
| Record conversion adds unnecessary complexity | Low | Keep conversion in one adapter helper and avoid repository/DTO abstractions until `channel-data` is specified |

## Scope discipline

Intentionally untouched in this plan:

- OAuth and token storage.
- YouTube collection and quota behavior.
- Workspace/tenant identity.
- Database schemas and migrations.
- HTTP endpoints and UI components.
- CI, packaging, formatter, linter, or type-checker setup.

## Open questions

No blocking questions remain for `analytics-core`. The four contract decisions
in the specification and the explicit silent-subscriber requirement are treated
as approved. Detailed tasks will be written to `tasks/todo.md` after this plan
is reviewed.
