# Plan: give the credential vault a life beyond the process, for nothing

Status on 2026-08-21: the mechanism is written and green; the two adapters that
carry it are the wrong ones and are replaced below. Nothing here has been
deployed. `web_ui/README.md` still lists the vault under "Still required before
production" and should keep saying so until this lands.

## What is wrong today

`GoogleCredentialStore` was a plain `dict`. Owners' refresh tokens sat in
process memory unencrypted, and a restart — a new revision, maintenance, a
crash, or scale-to-zero — took every one of them. The owner came back to an
application that had forgotten them.

## What is already done and stays

Committed on `feature/multi-channel-analytics`, backend-agnostic, 489 tests
green. None of it depends on which store is underneath:

- **Only the refresh token is written.** An access token lives an hour and can
  be asked for again; keeping it would multiply writes by every refresh and put
  a second live secret at rest for nothing. A restored slot comes back already
  expired, so the first call refreshes it — the path that runs hourly anyway.
- **A slot whose refresh token has not changed is not rewritten.** Google
  returns the same refresh token on most refreshes, so the hourly write becomes
  no write at all until the grant really changes. This is what makes a
  version-billed store viable below.
- **A document that cannot be read stops the start.** Coming up empty would tell
  every owner their channel is unlinked and invite them to authorize again while
  the credential sits there unread.
- **Persisting anywhere without a vault is refused at startup**, so a deployment
  cannot half-configure its way into writing tokens in the clear, or into
  looking durable while dropping every credential.
- `MetadataToken` in `web_ui/gcp.py`: the runtime service account's token from
  the metadata server, held until a minute before expiry. Both stores below need
  it.

## What changes, and why

`KmsEnvelope` and `GcsStateStore` in `web_ui/gcp.py` were written for this and
are **to be deleted**. Neither is free:

- Cloud KMS bills per active key version per month, and a key ring can never be
  removed from the project once created.
- The GCS always-free allowance is US regions only; this service is in
  `asia-northeast1`, and moving the bucket to the US puts a cross-region round
  trip in front of every request, because `workspace-access` rewrites its
  document on every authenticated page view.

Replace them with two stores that fit inside Google's always-free allowances.
Confirm the current figures when implementing — they move.

| What | Where | Why it fits |
|---|---|---|
| The credential document | **Secret Manager** | It is the managed vault `README` asks for: encrypted at rest, IAM-scoped, audit-logged. Free tier covers a handful of active versions and ten thousand access operations a month. Viable **only** because of the write-on-change rule above; the original hourly write would have buried it. |
| `workspace_access`, `channel_connections`, `channel_data`, `collection_jobs` | **Firestore, Native mode** | Free tier covers a day's reads, writes and deletes many times over at this scale. Handles the per-request write rate that Secret Manager cannot. |

Cloud Run's own free tier covers the request volume, so with `--min-instances 0`
the whole deployment can sit at zero.

## Steps

1. **Delete** `KmsEnvelope`, `GcsStateStore`, the `Envelope` protocol in
   `web_ui/google_provider.py`, and their tests. Keep `MetadataToken`.
2. **`SecretManagerStateStore`** in `web_ui/gcp.py`, satisfying the same
   `load()` / `save(document)` port:
   - `load` — access the latest enabled version; a missing secret reads as
     nothing, any other refusal is raised.
   - `save` — add a version, then destroy the ones it replaced. Old versions are
     what the free tier counts, and a stale refresh token is a live secret; both
     say to destroy rather than disable.
   - The secret is created by the deployment, not by this code, so the service
     account needs `secretmanager.versionAdder`, `versionDestroyer` and
     `secretAccessor` on that one secret and nothing else.
3. **`FirestoreStateStore`** in `web_ui/gcp.py`, same port. One document per
   module in one collection; the document is a single field holding the module's
   text. The store never learns what is inside, exactly as the port says.
4. **Rework `Durability`** in `web_ui/container.py`: it currently chooses
   between a bucket and a directory. It should choose between Firestore and a
   directory for modules, and name the Secret Manager secret for credentials.
   Keep the startup refusal — persisting module state while dropping credentials
   is the failure it exists to prevent.
5. **Drop the envelope requirement** in `GoogleCredentialStore`. Secret Manager
   is itself the boundary, so the guard becomes "the credential store must be
   the vault", not "the content must be sealed". Adjust
   `CredentialPersistenceTests` accordingly: the test that asserts no readable
   token reaches the store becomes a test that credentials go to the vault store
   and never to a module store.
6. **Deployment**, all reversible, no permanent resources:
   - `gcloud firestore databases create --location=asia-northeast1 --type=firestore-native`
     — Native mode cannot later become Datastore mode. This is the only
     one-way step, and it is a mode, not a bill.
   - `gcloud secrets create yna-owner-credentials` with an empty first version.
   - Grant the three roles above on that secret, and Firestore user on the
     database, to `yna-web@…` only.
   - Redeploy with `YNA_CREDENTIAL_SECRET`, `YNA_FIRESTORE_DATABASE`, and
     **`--min-instances 0`** — the always-on instance is what costs money today,
     and it stops being needed once state survives the instance.
7. **Verify against the deployment, not the tests.** Sign in, connect, collect,
   then force a new revision and confirm the owner is still connected and a
   collection runs without re-authorizing. That is the whole point, and it is
   the one thing unit tests cannot show. Check the Secret Manager version count
   afterwards: it should be one, not one per hour.
8. **Update `web_ui/README.md`**: move the vault out of "Still required before
   production", and correct "Keeping state across a restart", which currently
   says `YNA_STATE_DIR` buys nothing on Cloud Run.

## Left alone deliberately

Background workers for collection. Still the other item under "Still required
before production", still a separate decision.
