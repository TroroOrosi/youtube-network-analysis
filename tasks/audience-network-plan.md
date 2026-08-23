# Implementation Plan: audience-network-reporting

Specification: [`SPEC-audience-network-web.md`](../SPEC-audience-network-web.md)

## Overview

Generate one production-safe aggregate snapshot and one Excel report from the
existing offline datasets, validate them at the application boundary, publish
the analysis families through authenticated FastAPI routes, and deploy the
reviewed image to Cloud Run.

Module-specific plan files are used so the completed historical
`collection-jobs` plan in `tasks/plan.md` is not overwritten.

## Architecture decisions

- The production request path loads a committed bounded JSON artifact; pandas,
  networkx, openpyxl, and the 30+ MB raw CSV set remain outside the image.
- JSON and Excel are created together by one build-time command so headline
  values cannot drift.
- `web_ui.audience_report` validates the generated document and exposes only
  immutable presentation values.
- The page and workbook download both require an authenticated workspace with
  `analysis.read`.
- Browser-visible data contains aggregate counts and channel titles only.
- The demo/research report is a separate destination from live channel analysis.

## Dependency graph

```text
canonical CSV analyses
        |
        v
safe JSON snapshot + Excel workbook
        |
        v
validated runtime report service
        |
        +--> authenticated HTML route
        +--> authenticated Excel download
        |
        v
Docker image -> browser QA -> production deployment
```

## Task list

1. Freeze the aggregate artifact contract with failing tests.
2. Generate safe JSON and the matching Excel workbook.
3. Load and validate the report in the production runtime.
4. Publish the authenticated page, dashboard navigation, and Excel download.
5. Verify spreadsheet content, Docker/POSIX behavior, browser UX, security, and
   regression suites.
6. Run the requested standards/spec code review, fix findings, repeat gates,
   commit, push, deploy, and verify logs and rollback readiness.

## Checkpoints

- After tasks 1-2: generated artifacts reconcile with canonical CSV metrics and
  contain no raw identifiers.
- After tasks 3-4: focused unit and Web integration tests pass.
- After task 5: full local, Docker, spreadsheet, and browser evidence passes.
- After task 6: review findings are closed and production serves the new page
  and workbook with no new errors.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Demo metrics mistaken for live account data | High | Separate route and persistent source label |
| Raw audience identifiers published | Critical | Allowlisted generator schema plus negative leak tests |
| Image grows or request latency regresses | Medium | Ship only bounded JSON and workbook, never raw CSV |
| Web and workbook totals drift | High | Generate together and reconcile headline cells in tests |
| Large tables harm mobile/accessibility | Medium | Bounded top tables, summaries, and keyboard-scroll regions |
| Workbook is malformed | High | Open/render inspection and key-sheet checks before deployment |

## Open questions

None.
