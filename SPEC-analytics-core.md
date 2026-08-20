# Spec: analytics-core

Status: approved
Date: 2026-08-20
Capability map id: `analytics-core`

## Objective

Extract the subscriber segmentation and filtering behavior currently embedded
in `subscriber_analytics/extract_silent.py` into one pure, typed module. The CLI,
Notebook, future API, and future Web UI must call the same interface and produce
the same result for the same normalized input and reference time.

The module serves channel analysts and application adapters. It does not know
about Google OAuth, YouTube API calls, files, databases, workspaces, HTTP, or UI
rendering.

### User-visible behavior

- Determine the effective subscription time from YouTube's published time when
  available, otherwise from the first observed time.
- Classify every in-scope subscriber as `NEW_SILENT`, `OLD_SILENT`, `DORMANT`,
  or `ACTIVE`.
- Apply subscription-period, comment-activity, latest-observation, and segment
  filters with AND semantics.
- Reproduce the current silent-subscriber analysis for never-commented users,
  users with no comment inside a selected period, and the `NEW_SILENT` /
  `OLD_SILENT` breakdown.
- Return deterministic rows and segment summaries suitable for CLI, CSV, API,
  and UI adapters.
- Always report that only users with publicly visible subscriptions can appear
  in subscriber data.

## Contract

The public Python interface is record-based; pandas objects are not exposed.
Adapters may use pandas internally when converting existing CSV data.

```python
def analyze(
    subscribers: Sequence[SubscriberRecord],
    comment_activity: Sequence[CommentActivity],
    request: AnalysisRequest,
) -> AnalysisResult:
    """Return a deterministic analysis without mutating inputs or doing I/O."""
```

All public input and output records are immutable dataclasses. Enum values are
stable, language-independent machine identifiers. Japanese labels remain an
adapter concern so existing CSV and CLI output can remain compatible.

### Input records

`SubscriberRecord`

- `channel_id: str` — required, unique within the request.
- `title: str` — display title; may be empty.
- `api_published_at: datetime | None` — subscription time reported by YouTube.
- `first_seen_at: datetime` — required fallback subscription time.
- `last_seen_at: datetime` — required latest observation time for this record.

`CommentActivity`

- `author_channel_id: str` — required, unique within the activity input.
- `comment_count: int` — zero or greater.
- `last_comment_at: datetime | None` — required when `comment_count > 0`, and
  `None` when the count is zero.

Activity for channel IDs not present in `subscribers` is valid and ignored;
most commenters are not public subscribers.

`SegmentPolicy`

- `recent_subscriber_window: timedelta = 90 days`
- `recent_activity_window: timedelta = 90 days`

`AnalysisFilters`

- `subscribed_within: timedelta | None`
- `subscribed_since: date | None` — inclusive UTC calendar date.
- `subscribed_until: date | None` — inclusive UTC calendar date.
- `never_commented: bool = False`
- `no_comment_within: timedelta | None`
- `include_not_seen_latest: bool = False`
- `segments: frozenset[Segment]` — empty means all segments.

`AnalysisRequest`

- `reference_time: datetime` — required and timezone-aware.
- `filters: AnalysisFilters`
- `segment_policy: SegmentPolicy`

Adapters preserve current CLI behavior by setting the segment windows from
`--subscribed-within` and `--no-comment-within` when those flags are supplied;
otherwise both windows are 90 days.

### Output records

`AnalyzedSubscriber`

- all subscriber identity and observation fields;
- `subscribed_at: datetime`;
- `subscribed_at_source: API_PUBLISHED_AT | FIRST_SEEN_AT`;
- `is_in_latest_observation: bool`;
- `comment_count: int` and `last_comment_at: datetime | None`;
- `segment: Segment`.

`AnalysisResult`

- `rows: tuple[AnalyzedSubscriber, ...]` — filtered rows;
- `reference_time: datetime` normalized to UTC;
- `scope_count: int` — count after latest-observation scope selection and
  before the remaining filters;
- `filtered_count: int`;
- `excluded_not_seen_latest_count: int`;
- `scope_segment_counts: Mapping[Segment, int]`;
- `filtered_segment_counts: Mapping[Segment, int]`;
- `scope_silent_count: int` and `filtered_silent_count: int`, each equal to the
  corresponding `NEW_SILENT + OLD_SILENT` counts;
- `filters: AnalysisFilters` and `segment_policy: SegmentPolicy`;
- `limitations: tuple[LimitationCode, ...]`, initially always containing
  `PUBLIC_SUBSCRIPTIONS_ONLY`.

Mappings returned by the module are immutable views or immutable value objects.

### Segment rules

Rules are evaluated using `reference_time` and the two policy windows:

1. A subscriber with a comment inside the recent activity window is `ACTIVE`.
2. A subscriber with comments but none inside that window is `DORMANT`.
3. A subscriber with no comments whose effective subscription time is inside
   the recent subscriber window is `NEW_SILENT`.
4. All remaining subscribers with no comments are `OLD_SILENT`.

Boundary timestamps are inclusive: exactly on a cutoff counts as recent.

### Filter rules

- All specified filters are combined with AND semantics.
- `subscribed_within` includes timestamps exactly on the cutoff.
- `subscribed_since` begins at `00:00:00Z` on the supplied date.
- `subscribed_until` includes the whole supplied UTC date and is implemented as
  an exclusive cutoff at `00:00:00Z` on the following date.
- `never_commented` keeps rows whose count is zero.
- `no_comment_within` keeps both never-commented rows and rows whose last
  comment is older than the cutoff.
- `segments` is applied after segment assignment.
- Unless `include_not_seen_latest` is true, only rows whose `last_seen_at`
  equals the maximum `last_seen_at` in the request remain in scope. This is an
  observation rule, not proof that excluded users unsubscribed.

Rows are sorted by `subscribed_at` descending, then `channel_id` ascending.
This explicit tie-breaker makes results independent of input order.

### Error semantics

Invalid input raises `AnalysisValidationError` with stable `code`, `field`, and
human-readable `message` attributes. Initial error codes:

- `EMPTY_CHANNEL_ID`
- `DUPLICATE_SUBSCRIBER`
- `DUPLICATE_COMMENT_ACTIVITY`
- `INVALID_COMMENT_COUNT`
- `INCONSISTENT_COMMENT_ACTIVITY`
- `NAIVE_DATETIME`
- `INVALID_TIME_WINDOW`
- `INVALID_DATE_RANGE`

Errors never contain OAuth credentials, API keys, token values, or raw external
payloads.

## Tech stack

- Python 3.12.
- Standard-library `dataclasses`, `datetime`, and `enum` for the public contract.
- Existing pandas dependency may be used internally during the first extraction
  but must not appear in public type signatures.
- Existing `unittest` test runner; no new dependency in this module slice.

## Commands

```powershell
# Focused analytics-core tests
python -m unittest subscriber_analytics.tests.test_analytics_core -v

# Full subscriber analytics regression suite
python -m unittest discover -s subscriber_analytics/tests -v

# Python syntax/bytecode validation
python -m compileall -q subscriber_analytics

# Whitespace and patch integrity
git diff --check
```

There is currently no configured formatter, linter, type checker, or CI
workflow. Adding one is outside this module specification and requires a
separate reviewed change.

## Project structure

```text
subscriber_analytics/
  analytics_core.py                 # Pure public contract and implementation
  extract_silent.py                 # CLI/file adapter over analytics_core
  subscriber_analytics.ipynb        # Notebook adapter over analytics_core
  tests/
    test_analytics_core.py           # Pure unit and golden behavior tests
    test_analytics.py                # Existing collector/safety regressions
```

No database, Web framework, or frontend files are introduced by this module.

## Code style

Use immutable records, explicit UTC times, descriptive names, and one public
orchestration function:

```python
@dataclass(frozen=True)
class AnalysisRequest:
    reference_time: datetime
    filters: AnalysisFilters = AnalysisFilters()
    segment_policy: SegmentPolicy = SegmentPolicy()


def analyze(
    subscribers: Sequence[SubscriberRecord],
    comment_activity: Sequence[CommentActivity],
    request: AnalysisRequest,
) -> AnalysisResult:
    ...
```

Helpers remain private unless a second consumer needs a stable contract. Do not
accept `argparse.Namespace`, file paths, environment variables, pandas
DataFrames, or Google client objects at the public boundary.

## Testing strategy

Most tests are small, table-driven unit tests with no filesystem or network use.

- Golden parity test: a fixed registry/activity fixture must produce the same
  segment labels, filters, and primary ordering as the current CLI behavior.
- One test for each segment and every exact cutoff boundary.
- Tests for every individual filter and representative AND combinations.
- Tests for latest-observation inclusion/exclusion and the API-cap caveat.
- Tests for empty inputs, activity from non-subscribers, missing optional
  activity, duplicate IDs, invalid counts, invalid ranges, and naive times.
- Mutation test: input records and sequences remain unchanged.
- Determinism test: permuted input produces identical ordered output.
- Adapter regression: CLI output columns and Japanese segment labels remain
  unchanged after migration.

Each behavior change follows red-green-refactor. The full existing suite must
pass before the module is considered complete.

## Boundaries

### Always

- Normalize timezone-aware inputs to UTC once at the module boundary.
- Validate input invariants before analysis.
- Preserve existing CLI column names and Japanese labels in the CLI adapter.
- Keep computation deterministic and free of I/O and global state.
- Run focused and full tests before each implementation checkpoint.

### Ask first

- Add or upgrade dependencies.
- Change existing CLI flags, CSV columns, Japanese labels, or Notebook controls.
- Change the four segment meanings or their default 90-day windows.
- Introduce database, HTTP, OAuth, workspace, or frontend concerns.

### Never

- Call YouTube or any network service.
- Read environment variables, credentials, files, or databases.
- Mutate caller-owned inputs.
- Log or return secrets, raw OAuth data, or unredacted external payloads.
- Infer users whose subscriptions are private or describe observed counts as a
  complete subscriber total.

## Success criteria

1. `analytics_core.analyze` is the only public calculation entry point needed by
   CLI, Notebook, and future API adapters.
2. The four segment rules and all current CLI filters match golden fixtures.
   This includes never-commented extraction, no-comment-within extraction, and
   new/old silent totals.
3. Segment and filter boundaries are explicitly tested at exact cutoff times.
4. Results are identical for any permutation of equivalent input records.
5. Public contract types contain no pandas, argparse, filesystem, Google API,
   database, HTTP, or UI types.
6. Existing CLI CSV columns, Japanese segment labels, and command flags remain
   compatible after adapter migration.
7. Invalid input produces a stable typed error and no partial result.
8. The focused suite, full subscriber analytics suite, compile check, and
   `git diff --check` all pass.
9. The module performs no external I/O, confirmed by unit tests and code review.
10. User-facing result metadata states that only publicly visible subscriptions
    are observable.

## Review decisions requested

Please confirm these intentional contract choices before planning:

1. Public segment values use stable English machine IDs; adapters keep Japanese
   labels for the existing CLI/CSV.
2. The public interface uses immutable records rather than pandas DataFrames.
3. Equal subscription timestamps are ordered by `channel_id` for deterministic
   output, which may reorder tied rows compared with the current input order.
4. Invalid or timezone-naive input is rejected instead of silently coerced.
