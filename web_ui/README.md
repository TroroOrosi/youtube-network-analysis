# web-ui

Server-rendered hosted application for non-engineers: connect a channel, collect
data, read the segments, export a CSV.

Stack approved on 2026-08-21: FastAPI, Uvicorn, Jinja2 templates, no frontend
build step. Domain modules stay standard-library only; the dependencies in
[`requirements.txt`](requirements.txt) serve this layer alone.

## Running locally

```powershell
python -m pip install -r web_ui/requirements.txt
python -m uvicorn web_ui.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. Cookies are marked `Secure`, so a real
deployment must terminate TLS; for local review use a TLS proxy or a browser
profile that accepts secure cookies on localhost.

To operate the whole flow in a browser, serve it over TLS on the default port:

```powershell
$env:YNA_BASE_URL = "https://localhost"
python -m uvicorn web_ui.main:app --host 127.0.0.1 --port 443 `
  --ssl-keyfile dev.key --ssl-certfile dev.crt
```

The port matters: `channel-connections` rejects an authorization URL that
carries an explicit port, so a demo served on `https://localhost:8443` fails to
connect with `PROVIDER_AUTHORIZATION_FAILED`. Real Google authorization uses
`https://accounts.google.com` without a port, so the rule stays as it is.

## What is real and what is a demo

| Part | State |
|---|---|
| Sessions, workspaces, permissions | Real `workspace-access` module |
| Connection lifecycle, credential custody | Real `channel-connections` module |
| Collection runs, quota, retries | Real `collection-jobs` module |
| Segments, filters, export | Real `analytics-core` and `analysis-api` |
| Google OAuth and YouTube API | **Demo gateways in `demo_provider.py`** |
| Login identity provider | **Demo: a display name, no real IdP** |
| Storage | **In-memory: everything resets on restart** |

The demo consent screen says so on the page. No request leaves the process, no
real credential exists, and no YouTube quota is consumed.

## Security controls in this layer

- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, server-side revocable.
- CSRF: a `SameSite=Strict` double-submit token required on every POST.
- Security headers: CSP with `frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Errors render a stable Japanese message plus the next action, never a provider
  response, credential, or internal identifier.
- The workspace cookie is only a hint: every request re-resolves a real
  `WorkspaceContext` and the module checks the exact permission.

## Non-engineer requirements this satisfies

- One clear primary action per step: create workspace, connect, collect, analyse.
- Plain Japanese labels; the only technical term shown is the scope name, and it
  is explained next to it.
- Status, limitation, and next action shown together, including the public
  subscriptions limitation in the footer of every page.
- Semantic HTML with labelled controls, table headers, visible focus rings, and a
  responsive layout that works on a phone.

## Still required before production

Real OAuth client registration and consent verification, an approved identity
provider for login, a managed credential vault or KMS, persistent storage,
background workers for collection, rate limiting, and deployment TLS. Each is an
explicit later decision.

## Verification

```powershell
python -m unittest discover -s web_ui/tests -v
```
