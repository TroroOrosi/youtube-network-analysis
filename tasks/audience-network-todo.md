# Task List: audience-network-reporting

Plan: [`tasks/audience-network-plan.md`](audience-network-plan.md)
Spec: [`SPEC-audience-network-web.md`](../SPEC-audience-network-web.md)

## Task 1: Freeze the safe artifact contract

**Acceptance criteria:**
- [x] Tests define headline KPIs and every analysis family.
- [x] Tests reject viewer IDs, raw channel IDs, raw edges, paths, and secrets.

**Verification:** focused generator and report-model tests fail for the expected
missing implementation.

**Dependencies:** None

**Files:** generator test, report-model test, specification

## Task 2: Generate JSON and Excel together

**Acceptance criteria:**
- [x] One command regenerates both artifacts deterministically.
- [x] JSON contains bounded aggregate tables and source metadata only.
- [x] Workbook contains all analysis families and matching headline totals.

**Verification:** generator tests pass; a second run creates no diff.

**Dependencies:** Task 1

**Files:** generator, JSON artifact, Excel artifact, generator tests

## Task 3: Add the validated runtime reader

**Acceptance criteria:**
- [x] Missing or malformed artifacts fail closed at startup.
- [x] Presentation values are immutable and contain no raw document escape hatch.

**Verification:** report-model tests pass.

**Dependencies:** Task 2

**Files:** runtime reader and unit tests

## Task 4: Publish the authenticated UI and download

**Acceptance criteria:**
- [x] Dashboard links to the source-labelled report page.
- [x] All analysis families render accessibly and responsively.
- [x] Excel download requires permission and sends safe MIME/filename headers.

**Verification:** Web integration tests pass.

**Dependencies:** Task 3

**Files:** app route wiring, dashboard, report template, base styles, Web tests

## Task 5: Verify full artifacts and runtime

**Acceptance criteria:**
- [x] Workbook opens, key values reconcile, sheets/charts render legibly.
- [x] Full tests, changed-file lint, compile, Docker build, Linux nobody/POSIX, and browser QA pass.
- [x] Security checks find no raw identifiers or credentials in responses/image.

**Verification:** commands and results recorded in the progress checkpoint.

**Dependencies:** Task 4

## Task 6: Review, fix, save, and deploy

**Acceptance criteria:**
- [ ] Spec and standards reviews report no unresolved findings.
- [ ] Review fixes pass all gates.
- [ ] Atomic commits are pushed and the reviewed image reaches 100% production.
- [ ] Production browser, download, logs, and rollback target are verified.

**Verification:** clean worktree/upstream equality, Cloud Run revision/traffic,
HTTP/browser evidence, and zero relevant ERROR/500 entries.

**Dependencies:** Task 5
