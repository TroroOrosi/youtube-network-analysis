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
| Google OAuth and YouTube API | Real adapters in `google_provider.py`, used only when a client is registered; otherwise the demo gateways in `demo_provider.py` |
| Login identity provider | **Demo: a display name, no real IdP** |
| Credential vault | **In-memory: not encryption and not a KMS** |
| Storage | **In-memory: everything resets on restart** |

Without `YNA_GOOGLE_CLIENT_ID` and `YNA_GOOGLE_CLIENT_SECRET` the app stays on
the demo gateways: the demo consent screen says so on the page, no request
leaves the process, no real credential exists, and no YouTube quota is consumed.

## Connecting a real Google client

```powershell
$env:YNA_BASE_URL = "https://app.example"
$env:YNA_GOOGLE_CLIENT_ID = "<client id>.apps.googleusercontent.com"
$env:YNA_GOOGLE_CLIENT_SECRET = "<client secret>"
```

Both variables must be set; one alone keeps the demo provider, because a
half-configured client would send an owner to a consent screen that cannot
complete.

What the deployment owner has to do in Google Cloud first, in this order:

1. Create an OAuth 2.0 **Web application** client.
2. Register the redirect URI exactly: `<YNA_BASE_URL>/oauth/callback`.
3. Add the single scope `https://www.googleapis.com/auth/youtube.readonly`.
4. Enable the **YouTube Data API v3** for the project.
5. Submit the consent screen for verification. Until it is verified, only test
   users on the client can connect.

The adapter asks for offline access and an S256 PKCE challenge, refreshes the
access token when it is within a minute of expiry, and revokes the grant at the
provider when a connection is disconnected. Only the channel owner's own
account can connect: the exchange verifies the owner channel and probes
`subscriptions?myRecentSubscribers=true`, and a grant without that capability is
refused with `PROVIDER_CAPABILITY_MISSING`.

Nothing starts on its own. A real run still needs an owner to press 接続する and
then to consent at Google.

## Security controls in this layer

- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, server-side revocable.
- CSRF: a `SameSite=Strict` double-submit token required on every POST.
- Security headers: CSP with `frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Errors render a stable Japanese message plus the next action, never a provider
  response, credential, or internal identifier.
- The workspace cookie is only a hint: every request re-resolves a real
  `WorkspaceContext` and the module checks the exact permission.
- Rate limiting: 30 writes per minute per client, and 3 collection runs per
  minute, refused with a Japanese 429 page. Reads are never limited. The
  counters live in one process, so a multi-instance deployment needs a shared
  store; they are not the provider quota guard, which `collection-jobs` owns.
- `form-action` allows only this origin and `https://accounts.google.com`, the
  one host an owner is ever sent to.

## Non-engineer requirements this satisfies

- One clear primary action per step: create workspace, connect, collect, analyse.
- Plain Japanese labels; the only technical term shown is the scope name, and it
  is explained next to it.
- Status, limitation, and next action shown together, including the public
  subscriptions limitation in the footer of every page.
- Semantic HTML with labelled controls, table headers, visible focus rings, and a
  responsive layout that works on a phone.

## Deployment TLS

Every cookie is `Secure`, the OAuth redirect URI must be `https`, and Google
refuses a plain-http redirect for a web client, so TLS is not optional.

Terminate TLS at a reverse proxy (nginx, Caddy, a managed load balancer) and run
uvicorn behind it on loopback:

```powershell
python -m uvicorn web_ui.main:app --host 127.0.0.1 --port 8000 --proxy-headers
```

The proxy must set `X-Forwarded-Proto: https` and `X-Forwarded-For`, terminate
on the same host name as `YNA_BASE_URL`, and add HSTS. Without `--proxy-headers`
the rate limiter sees the proxy as the only client. Serving uvicorn's own TLS
(`--ssl-keyfile`/`--ssl-certfile`) is for local review only.

## Still required before production

An approved identity provider for login, a managed credential vault or KMS,
persistent storage, and background workers for collection. Each is an explicit
later decision.

## Verification

```powershell
python -m unittest discover -s web_ui/tests -v
```
