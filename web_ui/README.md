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
| Login identity provider | Real Google sign-in in `google_login.py` when a client is registered; otherwise a demo display name |
| Credential vault | **In-memory: not encryption and not a KMS** |
| Storage | In-memory unless `YNA_STATE_DIR` is set; see below |

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
2. Register both redirect URIs exactly: `<YNA_BASE_URL>/oauth/callback` for the
   channel grant and `<YNA_BASE_URL>/login/callback` for sign-in.
3. Add the single YouTube scope `https://www.googleapis.com/auth/youtube.readonly`,
   plus `openid`, `profile` and `email` for sign-in.
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

## Signing in

With a client registered, `/login` offers Google sign-in only: the display-name
door is refused with `LOGIN_METHOD_UNAVAILABLE`, because leaving it open would
let anyone claim an identity the provider is meanwhile verifying. Sign-in asks
for `openid profile email` and never for a YouTube scope, keeps no token after
the exchange, and hands `workspace-access` the subject Google vouched for. Each
attempt carries a single-use state that expires in ten minutes and lives in this
process only, so a restart cancels sign-ins in flight instead of honouring a
stale one. A refused consent, a replayed state and an unreachable Google all end
in the same message on purpose.

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

## Keeping state across a restart

Unset, the application starts clean and leaves nothing behind, which is what a
demo run should do. Point `YNA_STATE_DIR` at a directory and every module keeps
its state there instead:

```powershell
$env:YNA_STATE_DIR = "C:\ProgramData\yna-state"
```

- one document per module (`workspace_access.json`, `channel_connections.json`,
  `channel_data.json`, `collection_jobs.json`), so the modules stay
  independently extractable;
- each write is a rename over the previous document, so a process killed
  mid-save leaves the old one intact;
- the directory is created `0o700` and each document `0o600`. They hold no
  credential, but they do hold session digests and who may reach which
  workspace, so the directory belongs on a disk you would put a database on.
  **POSIX enforces those modes; Windows does not** — there a file inherits the
  directory's ACL, so a Windows host must restrict the directory itself;
- a document this code cannot read stops the start instead of silently
  beginning empty, which would show a live owner an unlinked channel and spend
  YouTube quota collecting data that is already there.

**Credentials are deliberately not kept.** The vault is still in memory, so
after a restart a connection is listed but must be authorized again before it
can collect. That ends when the KMS below is done, not before.

Two processes must not share one directory: each keeps the whole document in
memory and the last writer wins.

**The write cost is per request, not per write.** An authenticated page view
touches the session's idle expiry, which is a state change like any other, so
the document is rewritten for reads too: ten dashboard views were measured
rewriting `workspace_access.json` eleven times. That document holds every user,
session and audit event, so the cost per request grows with the deployment.
This is the ceiling the design accepts; a database is what removes it.

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

## Publishing it at a URL

[`Dockerfile`](../Dockerfile) in the repository root builds the whole
application into one image. It copies this layer, the five domain modules and
`subscriber_analytics/analytics_core.py` — the only file of that package this
layer imports, the rest of it pulls pandas and the Google client — installs
[`requirements.txt`](requirements.txt), and runs uvicorn as `nobody` on `$PORT`
with `--proxy-headers`.

```powershell
docker build -t yna-web .
docker run --rm -p 8099:8080 yna-web
```

Any host that runs a container and terminates TLS will serve it. Cloud Run is
the one this was written against, because the same Google Cloud project already
has to hold the OAuth client and the YouTube Data API:

```powershell
gcloud run deploy yna-web --source . --region asia-northeast1 `
  --allow-unauthenticated --max-instances 1 --min-instances 1 `
  --set-env-vars "FORWARDED_ALLOW_IPS=*"
```

Then take the `https://…run.app` URL it prints, redeploy with
`--set-env-vars "YNA_BASE_URL=<that URL>,FORWARDED_ALLOW_IPS=*"`, and register
that URL's two redirect URIs on the OAuth client. The URL is not knowable before
the first deploy, so two deploys is the shortest path, not a mistake.

Three flags are not preferences:

- `--max-instances 1`, because the credential vault, the rate-limit counters and
  the state document all live in one process. A second instance would show an
  owner a session or a connection that exists only in its sibling.
- `--min-instances 1`, because scaling to zero ends the process, and everything
  above is in memory. At zero a visitor returning after an idle period finds
  their workspace gone. This is the one line here that costs money; drop it and
  the demo still works, it just forgets.
- `FORWARDED_ALLOW_IPS=*`, because Cloud Run's front end is not on loopback and
  uvicorn trusts only loopback by default. It is safe **there** — the container
  has no address of its own and the front end overwrites `X-Forwarded-For`. On a
  host where the container is directly reachable, `*` lets any client forge its
  address and walk past the rate limiter; name the proxy instead.

`YNA_STATE_DIR` buys nothing on Cloud Run: the filesystem is memory that dies
with the instance. It is for a host with a real disk.

Left unset, `YNA_GOOGLE_CLIENT_ID` and `YNA_GOOGLE_CLIENT_SECRET` keep a public
deployment on the demo gateways, which is the honest thing to publish first —
the consent screen says it is a demo, no request leaves the process, no YouTube
quota is spent, and no real credential is ever held by the in-memory vault named
below.

### With a real Google client

The redirect URIs must match the deployed URL exactly, and the URL does not
exist until the first deploy, so the order is fixed:

1. Turn the APIs on, which is the only part of the Google side that gcloud can
   do:

   ```powershell
   gcloud services enable youtube.googleapis.com run.googleapis.com `
     secretmanager.googleapis.com
   ```

2. In the console — **not** gcloud; see below — create the Web application
   client, add the scopes, and set the consent screen Audience to Testing with
   the test users on it. Keep the client id and secret. Skip the redirect URIs,
   they need a URL that does not exist yet.
3. Deploy with the command below. It will start, because the client is
   configured; sign-in will not complete yet, because step 4 has not happened.
4. In the console again, register both redirect URIs on the client against the
   `run.app` URL the deploy printed: `<url>/oauth/callback` and
   `<url>/login/callback`.
5. Deploy again with `YNA_BASE_URL` set to that same URL. Sign-in works now.

Steps 2 and 4 have no command-line form. `gcloud iam oauth-clients` looks like
one and is not: it belongs to Workforce Identity Federation. `gcloud iap
oauth-brands` and `gcloud iap oauth-clients` belong to Identity-Aware Proxy, and
the API under them shut down in March 2026, which also retired the Terraform
`google_iap_brand` and `google_iap_client` resources. Client creation, the
consent screen and the test-user list are console-only; check whether that is
still true before building anything around it.

(An Audience of **Internal** would replace the test-user list with a Workspace
directory, but it admits only that organisation's accounts, so a list of
individual Gmail addresses needs Testing.)

### Who may sign in

Leave the consent screen in **Testing** and add each person to its test-user
list. Google then admits exactly that list and refuses everyone else at its own
sign-in page, before the redirect back here — so the guest list is Google's to
keep, not this application's, and there is no allow-list in this code to fall
out of date. This suits `run.app`, which is why the URL below needs no domain of
your own.

Opening it to everyone is a different job, not a bigger number: `youtube.readonly`
is a sensitive scope, so leaving Testing means Google's verification review, and
that review wants an authorized domain the applicant owns and can prove in
Search Console. `run.app` is Google's domain, not the applicant's. Everyone-can-
sign-in therefore needs a domain of your own mapped to the service, used as
`YNA_BASE_URL`, with both redirect URIs re-registered against it. Confirm the
requirements when submitting; this is Google policy, not something this code
decides.

**Set `YNA_REQUIRE_GOOGLE=1` on any deployment that is meant to be closed.**
Without a client this application does not stop — it falls back to the demo
gateways, and the demo door asks only for a display name, so a dropped
environment variable turns a test-user-only URL into one anyone can walk into.
It fails quietly, at the exact moment nobody is looking. With the flag set the
process refuses to start instead, and the revision never takes traffic.

The client secret does not belong in `--set-env-vars`. That writes it in clear
into the service configuration, where `gcloud run services describe` and the
console both read it back, and into shell history on the way. Keep it in Secret
Manager and mount it:

```powershell
"<client secret>" | gcloud secrets create yna-google-client-secret --data-file=-

gcloud iam service-accounts create yna-web
gcloud secrets add-iam-policy-binding yna-google-client-secret `
  --member "serviceAccount:yna-web@<project>.iam.gserviceaccount.com" `
  --role roles/secretmanager.secretAccessor

gcloud run deploy yna-web --source . --region asia-northeast1 `
  --allow-unauthenticated --max-instances 1 --min-instances 1 `
  --service-account "yna-web@<project>.iam.gserviceaccount.com" `
  --set-env-vars "YNA_BASE_URL=https://<service>.run.app,FORWARDED_ALLOW_IPS=*,YNA_REQUIRE_GOOGLE=1,YNA_GOOGLE_CLIENT_ID=<client id>.apps.googleusercontent.com" `
  --set-secrets "YNA_GOOGLE_CLIENT_SECRET=yna-google-client-secret:latest"
```

The service account is its own rather than the default compute one, which can
read every secret in the project; this one reads the single secret it needs.

**The host still logs the OAuth `code`.** Both callbacks receive it in the query
string, because an OAuth redirect is a GET and the session cookie is
`SameSite=Lax`, which a cross-site form post would not carry — so the code
cannot be moved into a body. The image runs uvicorn with `--no-access-log` to
avoid recording it a second time, but Cloud Run's own request log keeps the full
URL, and anyone who can read the project's logs can read it there. It is a
single-use code, expired within minutes, bound to a PKCE verifier and already
redeemed by the time it lands, so this is a residue rather than a hole. To drop
it as well, exclude those entries from the `_Default` sink:

```powershell
gcloud logging sinks update _Default `
  --log-filter='NOT (resource.type="cloud_run_revision" AND httpRequest.requestUrl=~"callback\?.*code=")'
```

That costs the request log for exactly the two paths most likely to need
debugging, which is why it is not the default here.

**What a hosted owner cannot guess, and should be told:** an instance that goes
away takes the whole application's memory with it. New revision, maintenance,
crash — `min-instances 1` makes that rarer, not impossible.

What is lost depends on `YNA_STATE_DIR`, and the two outcomes are different
enough to plan for:

- **Unset, which is what Cloud Run gets** — everything goes: sign-ins,
  workspaces, connections, collected data, and the tokens. An owner returns to
  an empty application and starts over. Nothing is left inconsistent, because
  nothing is left.
- **Set, on a host with a disk** — the modules come back, so a connection is
  listed and reads as connected, but the vault it points at is empty. The grant
  is gone while the record of it is not, and the next collection run fails until
  the owner presses 接続する and consents again.

Either way the fix is the same KMS named below, and until it exists this is the
behaviour to tell owners about rather than let them meet during a run.

## Still required before production

A managed credential vault or KMS and background workers for collection. Each
is an explicit later decision.

Persistent storage is now available but is not a database: `YNA_STATE_DIR`
keeps one JSON document per module and rewrites each in full, which is right
for a single process and wrong for two. The write cost is described below.

## Verification

```powershell
python -m unittest discover -s web_ui/tests -v
```

`test_the_documents_are_not_readable_by_other_accounts` skips on Windows, where
those modes mean nothing. It was last observed passing on `python:3.14-slim`
with `umask 0022`, which reported `0o700` for the directory and `0o600` for the
document.
