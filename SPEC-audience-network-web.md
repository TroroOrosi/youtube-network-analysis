# Spec: Audience network analysis in the hosted Web UI

Status: approved by explicit implementation request
Date: 2026-08-23
Module id: `audience-network-reporting`

## Objective

Publish the repository's existing audience-network analyses in the authenticated
hosted application and make the matching Excel report downloadable. The page is
for workspace members who need useful analysis even when a connected YouTube
account exposes too few public subscribers for live segmentation.

The hosted application must keep two datasets visibly distinct:

- Live channel analysis uses the signed-in workspace's collected YouTube data.
- Audience-network analysis uses the repository's existing anonymized research
  dataset and is labelled as demo/research data with its coverage and retrieval
  period.

Only aggregate results and channel titles may be published. Viewer identifiers,
raw viewer-channel edges, raw network edges, channel identifiers, credentials,
and filesystem paths must never be sent to the browser or included in the
downloadable workbook.

## Tech stack

- Python 3.14, FastAPI 0.141.1, Jinja2 3.1.6.
- Existing pandas/networkx/openpyxl offline analysis pipeline for build-time
  report generation only.
- Standard-library JSON loading in the production runtime; no pandas or
  analysis-time recomputation in Cloud Run.
- Existing session, workspace authorization, CSP, rate limiting, and secure
  response headers from `web_ui`.

## Commands

```powershell
# Generate the production-safe JSON snapshot and Excel report.
uv run --no-project --with-requirements requirements.txt `
  python scripts/export_web_audience_report.py

# Targeted tests.
$env:PYTHONPATH=(Get-Location).Path
uv run --no-project --with-requirements web_ui/requirements-dev.txt `
  --with pytest pytest -q web_ui/tests/test_audience_report.py `
  web_ui/tests/test_web.py

# Static quality checks.
uvx ruff check web_ui scripts
python -m compileall -q web_ui scripts
git diff --check

# Container verification.
docker build -t yna-web-review .
docker run --rm --user nobody yna-web-review `
  python -m unittest web_ui.tests.test_audience_report -v
```

## Project structure

- `scripts/export_web_audience_report.py`: build-time snapshot and workbook
  generator using the canonical offline analysis functions.
- `web_ui/data/audience-network-analysis.json`: production-safe aggregate
  snapshot committed with the source revision.
- `web_ui/assets/audience-network-analysis.xlsx`: generated, downloadable
  Excel workbook containing the same analysis families.
- `web_ui/audience_report.py`: validated, immutable runtime reader and view
  model builder.
- `web_ui/templates/audience_network.html`: accessible responsive report page.
- `web_ui/tests/test_audience_report.py`: snapshot schema and runtime unit tests.
- `web_ui/tests/test_web.py`: authenticated route, security, and download tests.

## Code style

Use immutable domain values and validate all generated input at the boundary:

```python
@dataclass(frozen=True)
class AudienceReportSummary:
    viewers: int
    channels: int
    viewer_channel_edges: int

    def __post_init__(self) -> None:
        if min(self.viewers, self.channels, self.viewer_channel_edges) <= 0:
            raise ValueError("audience report counts must be positive")
```

Routes remain thin: authorize, obtain the validated report from the service,
and render or download. Templates receive presentation-ready values and never
receive the raw JSON document.

## Testing strategy

- Generator tests reconcile headline totals and table row counts with the CSV
  sources and assert that forbidden raw identifiers are absent.
- Runtime unit tests cover malformed/missing artifacts and immutable view
  models.
- Web integration tests cover authentication, permission checks, page content,
  accessible table regions, source labelling, Excel MIME type and filename,
  and no leakage of raw identifiers or local paths.
- Existing full module suites, Docker execution as `nobody`, and a real-browser
  production pass are required before rollout.
- Excel verification checks workbook sheet names, chart presence, key headline
  cells, formula/error state where supported, and a visual render of all sheets.

## Boundaries

### Always

- Label the network report as anonymized demo/research data.
- Show dataset coverage and retrieval period near the page title.
- Authorize both page and Excel download with `analysis.read`.
- Serve a precomputed, bounded artifact; never load the 30+ MB raw CSVs on a
  request path.
- Escape all titles through Jinja and retain the application's CSP.
- Reconcile Web and Excel headline totals from one generated snapshot.

### Ask first

- Adding a database schema or new Cloud service.
- Publishing viewer-level or raw edge data.
- Replacing the live per-channel analysis with demo data.
- Adding a new third-party runtime dependency.

### Never

- Expose credentials, session material, viewer identifiers, raw channel IDs,
  raw edges, local paths, or private-subscription guesses.
- Present demo/research metrics as if they came from the signed-in account.
- Generate the Excel workbook on a Cloud Run request.
- weaken authentication, tenant checks, CSP, cache prevention, or download
  headers.

## Success criteria

1. An authenticated member can open `/audience-network` from the dashboard.
2. The page shows headline KPIs and all existing analysis families: top
   co-subscribed channels, viewer subscription breadth, interest categories,
   community sizes, strongest network relationships, affinity lift, and
   popularity-versus-affinity.
3. Each family has an accessible table or chart-like HTML visualization, clear
   labels, empty-safe rendering, and mobile horizontal scrolling where needed.
4. The page states that it uses anonymized demo/research data and displays 808
   viewers, 106,568 channels, 235,024 viewer-channel edges, 14,694 network
   nodes, 226,516 projected edges, and 50 communities.
5. The downloadable Excel workbook is regenerated, contains every analysis
   family, opens successfully, and matches the Web headline metrics.
6. Browser responses contain no viewer IDs, channel IDs, raw edges, local
   filesystem paths, credentials, or tokens.
7. Targeted and full tests, lint, compile checks, Docker/POSIX checks, code
   review, real-browser verification, deployment checks, and post-deploy logs
   all pass.

## Open questions

None. The explicit request to execute Excel work and publish all analyses is
treated as approval for this bounded aggregate-reporting capability.
