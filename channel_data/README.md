# channel-data

`channel_data` is the tenant-scoped, standard-library reference repository for
subscriber observations, cumulative subscriber history, video inventories,
aggregated comment activity, collection freshness, and silent-analysis input.
The approved contract and rationale live in [`SPEC-channel-data.md`](../SPEC-channel-data.md);
the implementation sequence lives in [`tasks/plan.md`](../tasks/plan.md).

This package is an in-memory fixture/reference. It does not connect YouTube,
store OAuth credentials, schedule jobs, expose HTTP, choose a database, or make
production transaction/encryption/backup guarantees.

## Public boundary

Use `ChannelDataService` through the protocols in `channel_data.ports`. Every
tenant operation requires the current trusted, single-workspace
`WorkspaceContext`; commands never accept a caller-supplied workspace ID.

| Operation | Required permission |
| --- | --- |
| Start, stage, or finish collection | `collection.run` |
| Freshness and collection history | `collection.read` |
| Registry, snapshots, and silent dataset | `analysis.read` |
| Channel deletion and workspace-scoped retention | `channel.manage_connection` |
| Workspace cascade | `workspace.delete` |

All public records are immutable/slotted. Inputs reject malformed identifiers,
oversized text/cursors, naive timestamps, booleans as counts, duplicate rows,
and unsupported coverage/status combinations. Timestamps normalize to UTC.

## Atomic collection flow

1. Call `start_collection` with a unique collection and idempotency key.
2. Stage exactly one subscriber snapshot or video inventory, or stage one
   aggregate comment replacement per accepted-inventory video.
3. Call `finish_collection(COMPLETE)` only when the candidate and progress
   counts are complete. This call performs the atomic promotion.

`PARTIAL` and `FAILED` attempts never replace an accepted generation. Readers
continue to see the last accepted success. Exact actor/payload/key replay
returns the original immutable result; changed use of a key fails safely.

Subscriber traversal completion means only that available provider pages were
exhausted. Every accepted subscriber snapshot carries both
`PUBLIC_SUBSCRIPTIONS_ONLY` and `PROVIDER_RESULT_CAP_POSSIBLE`; private
subscriptions remain unobservable and population completeness is never claimed.

## Silent-analysis readiness

`load_silent_analysis_dataset` fails closed unless all conditions hold:

- a complete subscriber snapshot is accepted;
- the accepted inventory has `OWNER_VIDEOS` scope;
- every video in that exact inventory has an accepted aggregate replacement,
  including explicit empty replacements for disabled/no-comment videos;
- comment coverage is complete for that inventory.

The stable readiness reasons are `NO_SUBSCRIBER_SNAPSHOT`,
`NO_VIDEO_INVENTORY`, `PUBLIC_VIDEO_SCOPE_ONLY`, and `COMMENTS_INCOMPLETE`.
Stored comment data contains only workspace/channel/inventory/video identifiers,
author channel ID, positive aggregate count, and latest timestamp. Comment text,
author display names, comment IDs, and reply/parent relationships are not stored.

## Pagination, retention, and deletion

List limits are 1-500 (default 100). Opaque one-time cursors are kept server-side
and bound to workspace, query, ordering, limit, and a captured immutable result.
Concurrent accepted changes therefore do not skip, repeat, or mix rows in an
active traversal. In-memory cursors do not survive process restart.

- Subscriber snapshot headers and observations expire at exactly 365 days.
- Terminal collection attempts and mutation idempotency records expire at
  exactly 90 days; unresolved in-progress attempts are not silently purged.
- Superseded video/comment generations are removed during atomic replacement.
- Channel/workspace cascades immediately remove every addressable in-memory
  copy and cursor while preserving identical IDs in other workspaces.
- A minimal data-free deletion tombstone supports exact retry and then follows
  the idempotency retention window.

Production persistence must reproduce these constraints transactionally and
add encryption, recoverable workspace cascade, legal-hold/backup expiry, and
background execution authority before any real data is stored.

## Verification

Run from the repository root:

```powershell
python -m unittest discover -s channel_data/tests -v
python -m unittest discover -s workspace_access/tests -v
python -m unittest discover -s subscriber_analytics/tests -v
python -m compileall -q channel_data workspace_access subscriber_analytics
python -c "import json,pathlib; p=pathlib.Path('subscriber_analytics/subscriber_analytics.ipynb'); nb=json.loads(p.read_text(encoding='utf-8')); [compile(''.join(c.get('source', [])), f'{p.name}:cell-{i}', 'exec') for i,c in enumerate(nb['cells']) if c.get('cell_type') == 'code']"
git diff --check
```

There is no configured formatter, linter, type checker, CI workflow, or new
dependency for this reference module.
