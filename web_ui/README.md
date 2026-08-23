# web-ui

Server-rendered hosted application for non-engineers: connect a channel, collect
data, read the segments, export a CSV.

Stack approved on 2026-08-21: FastAPI, Uvicorn, Jinja2 templates, no frontend
build step. Domain modules stay standard-library only; the dependencies in
[`requirements.txt`](requirements.txt) serve this layer alone.

## Running locally

```powershell
python -m pip install -r web_ui/requirements-dev.txt
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
| Credential vault | **In-memory unless `YNA_CREDENTIAL_SECRET` names a Secret Manager secret** |
| Storage | In-memory unless `YNA_STATE_DIR` or `YNA_FIRESTORE_DATABASE` is set; see below |

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
stale one. The same state is written to the browser as a `HttpOnly`,
`SameSite=Lax` cookie and has to come back with it: Google's answer says who
consented, not whose browser asked, so without that binding somebody could
finish their own consent and hand the callback URL to the owner, leaving the
owner signed in as them and connecting a channel into their workspace. A refused
consent, a replayed state, a state from another browser and an unreachable
Google all end in the same message on purpose.

## Security controls in this layer

- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, server-side revocable.
- CSRF: a `HttpOnly`, `SameSite=Strict` double-submit token required on every
  POST. The page carries the token because the server renders it into the
  form, so nothing here needs to read the cookie from script.
- Security headers: CSP with `frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Strict-Transport-Security` for a year so the first request of a session
  cannot be the plain http one that carries the cookies, and
  `Cache-Control: no-store` so no page is kept by a shared cache or handed back
  by the back button after a sign-out.
- `GET /collecting` is a read-only progress page. A CSRF-protected
  `POST /collecting/step` is the only browser action that advances a run or
  spends provider quota, so an image, prefetch, scanner, or cross-site form
  cannot start work.
- Errors render a stable Japanese message plus the next action, never a provider
  response, credential, or internal identifier.
- The workspace cookie is only a hint: every request re-resolves a real
  `WorkspaceContext` and the module checks the exact permission.
- Rate limiting: 30 writes per minute per client, and 3 collection write
  requests (queueing or advancing a run) per minute, refused with a Japanese
  429 page. Reads are never limited. A client is
  the last entry of `X-Forwarded-For`, which is the one the front end wrote and
  the caller cannot choose; behind Cloud Run every request otherwise has the
  same peer address, and counting that would let one visitor lock out everybody.
  The counters live in one process, so a multi-instance deployment needs a
  shared store; they are not the provider quota guard, which `collection-jobs`
  owns.
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
$env:YNA_CREDENTIAL_SECRET = "projects/<project>/secrets/yna-owner-credentials"
```

- one document per module (`workspace_access.json`, `channel_connections.json`,
  `channel_data.json`, `collection_jobs.json`, `analysis_api.json`), so the modules stay
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

**Persisting anything at all requires a vault for the credentials.** Set
`YNA_CREDENTIAL_SECRET` to the full resource name of a Secret Manager secret,
`projects/<project>/secrets/<secret>`, alongside either of the two stores here,
or the start is refused. Keeping the module documents while dropping the tokens
would leave every connection listed and unusable, and would look durable while
doing it — the refusal exists so that is not discovered a restart at a time.
Only the refresh token is written, so a restored connection comes back already
expired and refreshes on its first call.

On a host without a disk — Cloud Run — set `YNA_FIRESTORE_DATABASE` to
`projects/<project>/databases/(default)` instead of a directory. The same five
documents then live in a `state` collection, each holding the module's text in
one field. Saved analysis conditions live in `analysis_api` and therefore return
with the rest of the workspace after a restart.

Firestore caps a document a little under 1 MiB, and `channel_data` is the one
whose text grows with what was collected, so a module's text is split when it
passes `MAX_DOCUMENT_BYTES` (900,000). The head document, still named after the
module, holds the first piece and how many pieces there are; `channel_data~1`,
`channel_data~2` and so on hold the rest in order, and a read joins them back.
Every piece is cut on a character boundary and every piece is written in one
commit, which Firestore applies as a unit, so a save is still all or nothing
and a document is still text a reader can decode without this code. A module
that fits in one document is written exactly as it was before, which is what an
already-deployed database holds and what it keeps being read as.

What bounds a module is therefore the size of one commit — several megabytes —
rather than the size of one document.

Two processes must not share one directory: each keeps the whole document in
memory and the last writer wins.

**The write cost is per request, not per write.** An authenticated page view
touches the session's idle expiry, which is a state change like any other, so
the document is rewritten for reads too: ten dashboard views were measured
rewriting `workspace_access.json` eleven times. That document holds every user,
session and audit event, so the cost per request grows with the deployment.

Moving to a database did not remove that ceiling; it priced it. On Firestore
the same page view is one document write, against a free allowance of 20,000
writes a day. A module skips the write when its document comes out
byte-identical, but an idle expiry that moves on every authenticated request
never does — so that allowance, not a disk, is what a deployment opened
wider than its owners would meet first.

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
  --allow-unauthenticated --max-instances 1 --min-instances 0 `
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
- `--min-instances 0`, which is what the vault below buys: state survives the
  instance, so a visitor returning after an idle period finds their workspace
  where they left it. An always-on instance was the one line here that cost
  money, and it is no longer needed. A cold start still drops the rate-limit
  counters and any authorization in flight, so a sign-in or a consent
  interrupted halfway has to be started again.
- `FORWARDED_ALLOW_IPS=*`, because Cloud Run's front end is not on loopback and
  uvicorn trusts only loopback by default. It is safe **there** — the container
  has no address of its own and the front end overwrites `X-Forwarded-For`. On a
  host where the container is directly reachable, `*` lets any client forge its
  address and walk past the rate limiter; name the proxy instead.

`YNA_STATE_DIR` is for a host with a real disk: on Cloud Run the filesystem is
memory that dies with the instance. What this deployment sets instead is
`YNA_FIRESTORE_DATABASE` for the module documents and `YNA_CREDENTIAL_SECRET`
for the credentials, both below.

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
  --allow-unauthenticated --max-instances 1 --min-instances 0 `
  --service-account "yna-web@<project>.iam.gserviceaccount.com" `
  --set-env-vars "YNA_BASE_URL=https://<service>.run.app,FORWARDED_ALLOW_IPS=*,YNA_REQUIRE_GOOGLE=1,YNA_GOOGLE_CLIENT_ID=<client id>.apps.googleusercontent.com" `
  --set-secrets "YNA_GOOGLE_CLIENT_SECRET=yna-google-client-secret:latest"
```

Then the two stores state rests in, neither of which costs anything at this
scale:

```powershell
gcloud firestore databases create --location asia-northeast1 --type firestore-native
gcloud secrets create yna-owner-credentials

gcloud secrets add-iam-policy-binding yna-owner-credentials `
  --member "serviceAccount:yna-web@<project>.iam.gserviceaccount.com" `
  --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding yna-owner-credentials `
  --member "serviceAccount:yna-web@<project>.iam.gserviceaccount.com" `
  --role roles/secretmanager.secretVersionManager
gcloud projects add-iam-policy-binding <project> `
  --member "serviceAccount:yna-web@<project>.iam.gserviceaccount.com" `
  --role roles/datastore.user

gcloud run services update yna-web --region asia-northeast1 --min-instances 0 `
  --update-env-vars "YNA_CREDENTIAL_SECRET=projects/<project>/secrets/yna-owner-credentials,YNA_FIRESTORE_DATABASE=projects/<project>/databases/(default)"
```

Four things there are not arbitrary:

- **Native mode cannot later become Datastore mode.** It is the only one-way
  step in this procedure, and it is a mode, not a bill.
- **The database is the project's first, which is what carries the free tier.**
  The daily allowance does not depend on the location; only the price of
  exceeding it does.
- **`secretVersionManager`, because there is no `secretVersionDestroyer` role.**
  The store destroys the version it replaced — a superseded refresh token is a
  live secret while it can be read, and enabled versions are what the free tier
  counts — so it needs `versions.add`, `versions.list` and `versions.destroy`.
  The role that holds those, scoped to this one secret, is that one.
- **Both names are full resource paths**, so nothing in the process has to ask
  the platform which project it is running in.


The service account is its own rather than the default compute one, which can
read every secret in the project; this one reads the single secret it needs.

**Comments need an API key, not a bigger scope.** `commentThreads.list`
refuses `youtube.readonly` and wants `youtube.force-ssl`, which can also delete
comments and manage the account — too much to ask an owner for, to read what any
visitor can already read. Comments are public, so this layer reads them with a
server-side API key instead and leaves the owner's grant read-only:

```powershell
gcloud services api-keys create --display-name yna-youtube-public-reads `
  --api-target=service=youtube.googleapis.com
# then put its key string in Secret Manager as yna-youtube-api-key, grant the
# service account secretAccessor on it, and add to the deploy command:
#   --set-secrets "...,YNA_YOUTUBE_API_KEY=yna-youtube-api-key:latest"
```

Restrict the key to the YouTube Data API, as above. Without the key subscribers
and videos still collect; only the comment step fails, and because no credential
is involved on that path it cannot be mistaken for an expired grant.

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
away takes the process's memory with it, and at `--min-instances 0` that is
routine — after an idle period, on every new revision, on maintenance.

What survives is what was written: the five module documents in Firestore and
the owners' refresh tokens in Secret Manager. This includes saved analysis
conditions. A restored connection comes back with an expired access token and
refreshes it on its first call, which is the path that runs hourly anyway, so an
owner sees nothing.

What does not survive is the rate-limit counters and any authorization in
flight: a sign-in or a consent interrupted by the instance going away has to be
started again from the beginning.

Where neither store is configured — the reference default, and any host that
sets nothing — the older behaviour still applies: everything goes with the
instance, and an owner returns to an empty application and starts over.

## Collecting more than one request can hold

A channel's comments do not fit in one HTTP request, and past the daily quota
they do not fit in one day. So the collection is no longer done inside the
button press. Pressing **データを収集する** only queues two runs and sends the
browser to `/collecting`; the work happens in slices, and a run that cannot
finish today says when it can continue instead of failing.

Two things drive those slices, and they are the same code path:

**The browser.** `GET /collecting` shows progress without changing anything.
Its same-origin script submits a CSRF-protected form to `POST /collecting/step`,
which collects for up to `BROWSER_SLICE_SECONDS` (20 s) and redirects back to
the progress page. Without JavaScript the same form exposes a **収集を続ける**
button, so collection remains usable. Closing the tab costs only the slice in
flight: the runs stay queued and everything already collected is in
`channel-data`. When only a wait for tomorrow's quota is left, the page stops
and says so rather than spinning until midnight.

**Cloud Scheduler.** `POST /internal/drain` continues every workspace's queued
work with no session involved, which is the only way a run suspended for
tomorrow ever finishes if the owner never comes back. The route exists only
when `YNA_DRAIN_SERVICE_ACCOUNT` is set, and it believes exactly one caller:
the OIDC token on the request must be minted for that service account **and**
for this deployment's own drain URL. Google checks the signature, through
`tokeninfo`; this code checks who and what it was minted for. Every refusal is
a bodiless 401, so the endpoint tells a prober nothing — not even whether a
scheduler exists. The reply on success is a count and no workspace names.

The service itself stays public, because owners sign in through it with a
browser. That is why the token is checked in the application rather than left
to `roles/run.invoker`: a public URL has no invoker check to lean on.

Neither driver runs for ever in one call. The drain keeps working until its
slice is spent and then answers with how many runs it moved, and it stops early
when a pass moves nothing, so a queue that cannot be worked right now costs a
few milliseconds instead of two minutes of instance time. A single run holds
`collection-jobs` for at most twenty seconds at a time, so a page loaded while
the drain is running still answers.

Both drivers also do the tidying that the stored state needs: the drain asks
`collection-jobs` to drop what is past its retention, and the browser driver
asks `channel-data` and `channel-connections` the same when a collection
finishes, under the owner's own authority. Retention periods are unchanged;
they simply have a caller now. Each module keeps its whole state as one stored
text that is rewritten in full on every change, so records nobody removes are
paid for on every write, and past a few megabytes they stop the write outright.

### Setting up the scheduler

```powershell
$PROJECT = (gcloud config get-value project)
$SERVICE_URL = (gcloud run services describe yna-web `
  --region asia-northeast1 --format "value(status.url)")
$CALLER = "yna-scheduler@$PROJECT.iam.gserviceaccount.com"

gcloud iam service-accounts create yna-scheduler `
  --display-name "YNA scheduled drain"

gcloud run services update yna-web --region asia-northeast1 `
  --update-env-vars "YNA_DRAIN_SERVICE_ACCOUNT=$CALLER"

gcloud scheduler jobs create http yna-drain `
  --location asia-northeast1 `
  --schedule "7 * * * *" `
  --time-zone UTC `
  --uri "$SERVICE_URL/internal/drain" `
  --http-method POST `
  --oidc-service-account-email $CALLER `
  --oidc-token-audience "$SERVICE_URL/internal/drain" `
  --attempt-deadline 180s
```

The service account is given no roles at all. It needs none: the only thing it
does is be named in a token, and the only thing that reads that name is this
application. `--oidc-token-audience` must match the URL exactly, because that is
the half of the check that stops a token issued for some other service being
replayed here.

Hourly is generous for what the job is for. The only thing it can unblock is a
run waiting for units, and units refill once a day at UTC midnight; the extra
23 calls are there so a suspension that happens for another reason is not
waiting a whole day.

### What it costs

Nothing, and here is the arithmetic rather than the assurance:

- **Cloud Scheduler** allows 3 jobs free per billing account. This is one.
- **Cloud Run** allows 180,000 vCPU-seconds a month. An hourly call that finds
  nothing queued answers immediately: 720 calls a month of well under a second
  each, which is a rounding error. A call that does find work is capped at
  `DRAIN_SLICE_SECONDS` (120 s), so even the impossible case where all 720 ran
  the full slice is 86,400 vCPU-seconds — still inside the allowance, though it
  would leave under half of it for people using the site. The daily quota stops
  collection long before that, so it is a ceiling and not an expectation.
- **Firestore** allows 20,000 writes a day. A slice writes the module documents
  it touched, once per slice, not once per provider call. A module whose text
  has grown past one document counts as one write per piece, so a `channel_data`
  of three pieces spends three of that allowance per save.
- The scheduler job itself makes no image, holds no storage, and adds no
  always-on instance: `--min-instances 0` still stands, and the drain simply
  cold-starts.

If the drain is not set up, nothing breaks. Collections still finish while the
browser is on `/collecting`, and one that runs out of units waits for an owner
to come back and press the button again.

### The build leaves rubbish behind

`gcloud run deploy --source` builds an image into Artifact Registry and uploads
the sources it built from into a bucket, and it keeps every one of them. Neither
is needed once the revision exists — the image the serving revision runs is the
only one that has to survive, and the sources are in git. Left alone they are
what fills the 0.5 GB Artifact Registry allowance, and the source bucket is
regional storage in `asia-northeast1`, which the always-free 5 GB does not cover
because that allowance is US-only.

Rather than remembering to delete them, let the platform do it:

```powershell
# policy.json
# [
#   {"name": "keep-recent-images", "action": {"type": "Keep"},
#    "mostRecentVersions": {"keepCount": 3}},
#   {"name": "delete-superseded-images", "action": {"type": "Delete"},
#    "condition": {"tagState": "any", "olderThan": "1d"}}
# ]
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy `
  --location asia-northeast1 --policy=policy.json --no-dry-run

# lifecycle.json
# {"lifecycle": {"rule": [{"action": {"type": "Delete"},
#                          "condition": {"age": 3}}]}}
gcloud storage buckets update gs://run-sources-<project>-asia-northeast1 `
  --lifecycle-file=lifecycle.json
```

Keep rules win over delete rules, so the three newest images survive whatever
their age: the serving revision, the one before it, and one more. Older images
go, and with them the ability to roll back to the revisions that ran them —
those revisions stay listed and stop being startable. `--dry-run` instead of
`--no-dry-run` writes what it would have deleted to the logs and deletes
nothing.

## Still required before production

Background workers for collection are done, in the smallest form that works: no
queue, no worker platform, no daemon. A run is sliced, and two callers drive the
slices — the browser that started it, and one scheduled call for the days it
cannot finish in. See the section above.

The credential vault is done and deployed: the module documents rest in
Firestore, the owners' refresh tokens in one Secret Manager secret, and a new
revision no longer asks anyone to authorize again. Verified against the
deployment on 2026-08-22 — sign in, connect, collect, force a new revision,
collect again without consenting, with the secret still holding exactly one
version afterwards. [`PLAN-credential-vault.md`](../PLAN-credential-vault.md)
holds the design and why these two stores rather than KMS and a bucket.

Storage is a database now, but the shape it is used in has not changed: one
document per module, rewritten in full on every change, which is right for a
single process and wrong for two — hence `--max-instances 1`. The write cost is
described below.

## Verification

```powershell
python -m unittest discover -s web_ui/tests -t web_ui/tests -v
```

`test_the_documents_are_not_readable_by_other_accounts` skips on Windows, where
those modes mean nothing. It was last observed passing on `python:3.14-slim`
with `umask 0022`, which reported `0o700` for the directory and `0o600` for the
document.
