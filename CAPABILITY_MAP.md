# Capability Map: Hosted YouTube Analytics

Status: approved
Date: 2026-08-20

## Product direction

Turn the existing single-channel `subscriber_analytics` CLI/Notebook into a
hosted application that multiple users can use with multiple YouTube channels.
The application keeps collection, analytics, and presentation separate so the
CLI, Notebook, API, and Web UI share one interpretation of every filter and
segment.

## Assumptions for review

1. The target product is a hosted Web application, not only a local desktop app.
2. A workspace may contain multiple users and multiple connected channels.
3. YouTube access stays read-only (`youtube.readonly`) in the MVP.
4. Subscriber analysis remains limited to publicly visible subscriptions and
   must clearly report that limitation.
5. Real OAuth, cloud deployment, and production credentials are outside the
   first implementation slice; fixture data is used until security ADRs are
   approved.

## Modules

| Module id | Responsibility | Depends on |
|---|---|---|
| `analytics-core` | Pure filter, segmentation, summary, and export-ready result calculation with no Google API, database, or UI dependency | — |
| `workspace-access` | Users, sessions, workspaces, memberships, roles, and tenant authorization rules | — |
| `channel-data` | Tenant-scoped subscriber snapshots, registry entries, videos, comment activity, and collection state repositories | `workspace-access` |
| `channel-connections` | Web OAuth lifecycle, connected-channel identity, scope checks, encrypted credential references, and revocation | `workspace-access` |
| `collection-jobs` | Quota-aware subscriber/comment synchronization, retries, incremental caches, schedules, and run status | `channel-connections`, `channel-data` |
| `analysis-api` | Workspace-authorized analysis commands, saved views, comparisons, and CSV/Excel export contracts | `workspace-access`, `channel-data`, `analytics-core` |
| `web-ui` | Channel switcher, connection/sync status, filter controls, segment KPIs, tables, charts, and exports | `channel-connections`, `collection-jobs`, `analysis-api` |

## Dependency direction

```text
analytics-core ───────────────────────────────┐
                                              v
workspace-access -> channel-data --------> analysis-api -> web-ui
        |                ^                     ^             ^
        v                |                     |             |
channel-connections -> collection-jobs -------+-------------+
```

No module may query another workspace's data. `workspace-access` supplies the
authorization decision; storage adapters enforce the same `workspace_id`
boundary in every read and write.

## Build order

1. `analytics-core` — freeze current CLI behavior with golden regression tests.
2. `workspace-access` — specify tenant/session invariants before persistence.
3. `channel-data` — define repositories and migrations behind tenant-scoped
   interfaces.
4. `channel-connections` — approve threat model and ADR before Web OAuth code.
5. `collection-jobs` — adapt the existing collectors to connection/data ports.
6. `analysis-api` — expose a fixture-backed vertical slice first, then database
   adapters.
7. `web-ui` — implement filters and results against the stable API contract.

`analytics-core` can be built independently while security-sensitive identity
and OAuth specifications are reviewed. The first visible vertical slice is
`analytics-core` -> fixture adapter -> `analysis-api` -> `web-ui`; it contains
no production credentials and does not contact YouTube.

## MVP boundaries

### Included

- Multiple workspace members and connected YouTube channels.
- Read-only Web OAuth connection and revocation.
- Manual and scheduled subscriber/comment synchronization.
- Channel selection and comparison.
- Subscription period, subscriber status, comment activity, and segment filters.
- Segment KPIs, result table, basic charts, CSV/Excel export.
- Collection freshness, coverage, quota, progress, and error visibility.
- Tenant authorization, audit events, retention/deletion, and token revocation.

### Not included in the MVP

- Uploading, editing, moderating, or deleting YouTube content.
- Inferring private subscriptions that the YouTube API does not expose.
- Billing, public marketplace integrations, or native mobile applications.
- Production deployment before OAuth/credential/tenant-isolation review gates
  pass.

## Approval gate

After this map is approved, create one specification per module using the stable
module ids above. Start with `SPEC-analytics-core.md`; do not implement Web OAuth
or token persistence until `workspace-access` and `channel-connections` specs,
the threat model, and related ADRs are approved.
