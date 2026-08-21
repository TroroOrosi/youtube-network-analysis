# Analysis API Specification

Status: Approved
Module id: `analysis-api`
Date: 2026-08-21

## Objective

Expose workspace-authorized analysis over accepted `channel-data` generations:
run a filtered analysis, page its rows, save and reuse views, compare channels,
and export a CSV document — without duplicating any analytics rule and without
touching a provider, credential, or job.

`analytics-core` remains the only place segments and filters are computed. This
module resolves permissions, loads the accepted dataset, maps it into the core,
and shapes safe results for an HTTP or UI adapter.

## Permissions

| Operation | Permission |
|---|---|
| Run analysis, page rows, compare, read views | `analysis.read` |
| Save or delete a view | `analysis.read` |
| Export a document | `analysis.export` |
| Workspace view cascade | `workspace.delete` |

Views are workspace-shared configuration, not per-user state.

## Behaviour

- An analysis names one channel and a filter set. The module loads the accepted
  silent-analysis dataset for that channel and fails closed with the exact
  `channel-data` readiness reason when the dataset is not ready.
- Every result carries the mandatory limitations of the underlying data:
  public subscriptions only, and a possible provider result cap.
- Rows are paged with opaque single-use cursors bound to workspace, query, and
  the accepted snapshot and inventory identifiers. A newer accepted generation
  expires the cursor instead of mixing generations.
- A comparison runs the same filters across up to five channels of the same
  workspace and returns their summaries side by side. A channel whose dataset is
  not ready is reported by its readiness reason rather than failing the whole
  comparison.
- An export produces a UTF-8 CSV document with a BOM for spreadsheet software,
  a deterministic column order, and a filename derived from the channel and the
  reference time. Export requires the separate export permission.

## Filters

The public filter input mirrors `analytics-core` exactly and adds no new rule:
`subscribed_within_days`, `subscribed_since`, `subscribed_until`,
`never_commented`, `no_comment_within_days`, `include_not_seen_latest`, and a
segment allowlist. Values are validated at the boundary and converted into the
core's own filter type.

## Stable safe errors

`INVALID_INPUT`, `PERMISSION_DENIED`, `VIEW_NOT_FOUND_OR_FORBIDDEN`,
`DATASET_NOT_READY`, `IDEMPOTENCY_CONFLICT`, `INVALID_CURSOR`,
`CURSOR_EXPIRED`.

`DATASET_NOT_READY` carries the stable `channel-data` reason code so a UI can
tell the user what to collect next. No error exposes provider detail, another
workspace's existence, or internal identifiers.

## Boundaries

### Always

- Resolve the exact permission on a trusted context before reading anything.
- Delegate every segment, filter, and boundary rule to `analytics-core`.
- Report the mandatory data limitations with every summary and export.
- Keep view, cursor, and idempotency keys prefixed by workspace.

### Never

- Recompute or override an analytics rule locally.
- Read another workspace's dataset, views, or cursors.
- Contact a provider, hold a credential, or start a collection.

## Success criteria

1. Every operation requires a trusted context and the exact permission.
2. Results and exports match `analytics-core` for the same inputs.
3. Readiness failures carry the exact `channel-data` reason.
4. Pagination is deterministic, opaque, and generation bound.
5. Views are workspace scoped, idempotent, and safely deletable.
6. Exports are byte-deterministic for a fixed dataset and reference time.
7. No dependency, provider call, credential, or job behaviour is introduced.
