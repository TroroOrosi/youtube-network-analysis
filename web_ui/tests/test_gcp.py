"""The Google Cloud adapters: the identity, the vault, the module documents."""

from __future__ import annotations

import base64
import json
import time
import tempfile
import unittest
from pathlib import Path

from web_ui.container import Durability, build_services, durability_from_env
from web_ui.gcp import (
    FIRESTORE_ROOT,
    SECRET_MANAGER_ROOT,
    FirestoreStateStore,
    MAX_DOCUMENT_BYTES,
    GcpUnavailable,
    MetadataToken,
    ScheduledCaller,
    SecretManagerStateStore,
    TOKENINFO_URL,
)
from web_ui.tests.test_google_provider import CONFIG, FakeTransport, credential


def token() -> str:
    return "sa-token"


class MetadataTokenTests(unittest.TestCase):
    def replies(self, expires_in: int = 3600) -> dict[str, tuple[int, bytes]]:
        body = {"access_token": "at-metadata", "expires_in": expires_in}
        return {"http://metadata.google.internal": (200, json.dumps(body).encode())}

    def test_it_asks_the_metadata_server_with_the_required_header(self) -> None:
        transport = FakeTransport(self.replies())
        self.assertEqual(MetadataToken(transport=transport)(), "at-metadata")
        _, _, headers, _ = transport.requests[0]
        self.assertEqual(headers["Metadata-Flavor"], "Google")

    def test_it_holds_the_token_instead_of_asking_again(self) -> None:
        transport = FakeTransport(self.replies())
        source = MetadataToken(transport=transport, now=lambda: 0.0)
        source()
        source()
        self.assertEqual(len(transport.requests), 1)

    def test_it_asks_again_once_the_token_is_nearly_spent(self) -> None:
        transport = FakeTransport(self.replies())
        clock = [0.0]
        source = MetadataToken(transport=transport, now=lambda: clock[0])
        source()
        clock[0] = 3600.0
        source()
        self.assertEqual(len(transport.requests), 2)

    def test_a_refusal_is_raised_and_quotes_no_credential(self) -> None:
        transport = FakeTransport({"http://metadata": (403, b"denied")})
        with self.assertRaises(GcpUnavailable) as raised:
            MetadataToken(transport=transport)()
        self.assertNotIn("denied", str(raised.exception))


SECRET = "projects/p/secrets/yna-owner-credentials"
SECRET_URL = f"{SECRET_MANAGER_ROOT}/{SECRET}"


def access_reply(document: str | None) -> tuple[int, bytes]:
    data = "" if document is None else base64.b64encode(document.encode()).decode()
    return 200, json.dumps({"payload": {"data": data}}).encode()


def version_list(*numbers: int, following: str | None = None) -> tuple[int, bytes]:
    body: dict[str, object] = {
        "versions": [{"name": f"{SECRET}/versions/{number}"} for number in numbers]
    }
    if following is not None:
        body["nextPageToken"] = following
    return 200, json.dumps(body).encode()


class SecretManagerStateStoreTests(unittest.TestCase):
    """The vault: one secret, one readable version, the rest destroyed."""

    def store(self, replies: dict[str, tuple[int, bytes]]):
        transport = FakeTransport(replies)
        return SecretManagerStateStore(SECRET, token, transport=transport), transport

    def saving_replies(self, *enabled: int) -> dict[str, tuple[int, bytes]]:
        return {
            f"{SECRET_URL}:addVersion": (
                200,
                json.dumps({"name": f"{SECRET}/versions/2"}).encode(),
            ),
            f"{SECRET_URL}/versions?": version_list(*enabled),
            f"{SECRET_URL}/versions/": (200, b"{}"),
        }

    def test_a_secret_with_no_readable_version_reads_as_nothing(self) -> None:
        store, _ = self.store({SECRET_URL: (404, b"")})
        self.assertIsNone(store.load())

    def test_it_returns_the_stored_document(self) -> None:
        store, transport = self.store({SECRET_URL: access_reply('{"a":1}')})
        self.assertEqual(store.load(), '{"a":1}')
        _, url, _, _ = transport.requests[0]
        self.assertTrue(url.endswith("/versions/latest:access"))

    def test_an_empty_first_version_reads_as_nothing(self) -> None:
        """A deployment may create the secret with an empty version first."""

        store, _ = self.store({SECRET_URL: access_reply(None)})
        self.assertIsNone(store.load())

    def test_an_unreadable_vault_stops_instead_of_starting_empty(self) -> None:
        """Starting empty would invite owners to re-authorize a live grant."""

        store, _ = self.store({SECRET_URL: (503, b"")})
        with self.assertRaises(GcpUnavailable):
            store.load()

    def test_saving_adds_a_version_carrying_the_document(self) -> None:
        store, transport = self.store(self.saving_replies(2))
        store.save('{"a":1}')
        method, url, _, body = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith(":addVersion"))
        sent = json.loads(body or b"")["payload"]["data"]
        self.assertEqual(base64.b64decode(sent).decode(), '{"a":1}')

    def test_the_version_it_replaced_is_destroyed_and_the_new_one_kept(self) -> None:
        """A superseded refresh token stays live until the version is gone."""

        store, transport = self.store(self.saving_replies(1, 2))
        store.save("{}")
        destroyed = [url for _, url, _, _ in transport.requests if ":destroy" in url]
        self.assertEqual(destroyed, [f"{SECRET_URL}/versions/1:destroy"])

    def test_a_version_on_the_second_page_is_destroyed_too(self) -> None:
        """A version left enabled is a live refresh token, wherever it listed."""

        replies = self.saving_replies(1, 2)
        replies = {
            f"{SECRET_URL}/versions?filter=state%3AENABLED&pageToken=page-2": (
                version_list(3)
            ),
            **replies,
        }
        replies[f"{SECRET_URL}/versions?"] = version_list(1, 2, following="page-2")
        store, transport = self.store(replies)
        store.save("{}")
        destroyed = [url for _, url, _, _ in transport.requests if ":destroy" in url]
        self.assertEqual(
            destroyed,
            [f"{SECRET_URL}/versions/1:destroy", f"{SECRET_URL}/versions/3:destroy"],
        )

    def test_nothing_is_destroyed_before_the_listing_ends(self) -> None:
        """Destroying mid-walk shortens the filter under the page cursor."""

        replies = self.saving_replies(1, 2)
        replies = {
            f"{SECRET_URL}/versions?filter=state%3AENABLED&pageToken=page-2": (
                version_list(3)
            ),
            **replies,
        }
        replies[f"{SECRET_URL}/versions?"] = version_list(1, 2, following="page-2")
        store, transport = self.store(replies)
        store.save("{}")
        kinds = [
            "list" if "/versions?" in url else "destroy"
            for _, url, _, _ in transport.requests
            if "/versions?" in url or ":destroy" in url
        ]
        self.assertEqual(kinds, ["list", "list", "destroy", "destroy"])

    def test_housekeeping_that_fails_does_not_fail_the_save(self) -> None:
        """The credential is already stored; the next save lists again."""

        replies = self.saving_replies(1, 2)
        replies[f"{SECRET_URL}/versions?"] = (500, b"")
        store, _ = self.store(replies)
        store.save("{}")

    def test_a_refused_write_is_raised(self) -> None:
        store, _ = self.store({f"{SECRET_URL}:addVersion": (403, b"denied")})
        with self.assertRaises(GcpUnavailable) as raised:
            store.save("{}")
        self.assertNotIn("denied", str(raised.exception))


DATABASE = "projects/p/databases/(default)"
DOCUMENT_URL = f"{FIRESTORE_ROOT}/{DATABASE}/documents/state/channel_data"
COMMIT_URL = f"{FIRESTORE_ROOT}/{DATABASE}/documents:commit"


class FirestoreStateStoreTests(unittest.TestCase):
    def store(self, replies: dict[str, tuple[int, bytes]]):
        transport = FakeTransport(replies)
        return (
            FirestoreStateStore(DATABASE, "channel_data", token, transport=transport),
            transport,
        )

    def held(self, document: str, parts: int = 0) -> tuple[int, bytes]:
        """A document as Firestore answers it; no count is one written before."""

        fields: dict[str, object] = {"document": {"stringValue": document}}
        if parts:
            fields["parts"] = {"integerValue": str(parts)}
        return 200, json.dumps({"fields": fields}).encode()

    def chain(self, *pieces: str) -> dict[str, tuple[int, bytes]]:
        """A split text as the documents holding it, parts named before the head.

        `FakeTransport` answers the first key the url starts with, and the head
        url is a prefix of every part url, so the parts have to come first.
        """

        replies = {
            f"{DOCUMENT_URL}~{index}": self.held(piece)
            for index, piece in enumerate(pieces[1:], start=1)
        }
        replies[DOCUMENT_URL] = self.held(pieces[0], len(pieces))
        return replies

    def written(self, transport) -> list[dict]:
        method, url, _, body = transport.requests[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, COMMIT_URL)
        return json.loads(body or b"")["writes"]

    def test_a_missing_document_reads_as_nothing_rather_than_failing(self) -> None:
        store, _ = self.store({DOCUMENT_URL: (404, b"")})
        self.assertIsNone(store.load())

    def test_it_returns_the_module_text_unchanged(self) -> None:
        store, _ = self.store({DOCUMENT_URL: self.held('{"a":1}')})
        self.assertEqual(store.load(), '{"a":1}')

    def test_an_unreadable_database_stops_instead_of_starting_empty(self) -> None:
        store, _ = self.store({DOCUMENT_URL: (503, b"")})
        with self.assertRaises(GcpUnavailable):
            store.load()

    def test_a_document_holding_something_else_is_refused(self) -> None:
        empty = (200, json.dumps({"fields": {}}).encode())
        store, _ = self.store({DOCUMENT_URL: empty})
        with self.assertRaises(GcpUnavailable):
            store.load()

    def test_saving_writes_the_text_as_the_one_field(self) -> None:
        store, transport = self.store({COMMIT_URL: (200, b"{}")})
        store.save('{"a":1}')
        _, _, headers, _ = transport.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer sa-token")
        self.assertEqual(
            self.written(transport),
            [
                {
                    "update": {
                        "name": f"{DATABASE}/documents/state/channel_data",
                        "fields": {
                            "document": {"stringValue": '{"a":1}'},
                            "parts": {"integerValue": "1"},
                        },
                    }
                }
            ],
        )

    def test_a_refused_write_is_raised(self) -> None:
        store, _ = self.store({COMMIT_URL: (403, b"")})
        with self.assertRaises(GcpUnavailable):
            store.save("{}")

    def test_text_past_the_cap_is_split_across_documents_in_one_write(self) -> None:
        """The 1 MiB a document holds is not the ceiling on a module."""

        document = "あ" * MAX_DOCUMENT_BYTES
        store, transport = self.store({COMMIT_URL: (200, b"{}")})
        store.save(document)
        writes = self.written(transport)
        self.assertEqual(len(transport.requests), 1)
        self.assertGreater(len(writes), 1)
        names = [write["update"]["name"] for write in writes]
        self.assertEqual(names[0], f"{DATABASE}/documents/state/channel_data")
        self.assertEqual(names[1], f"{DATABASE}/documents/state/channel_data~1")
        pieces = [
            write["update"]["fields"]["document"]["stringValue"] for write in writes
        ]
        self.assertEqual("".join(pieces), document)
        self.assertEqual(
            writes[0]["update"]["fields"]["parts"],
            {"integerValue": str(len(writes))},
        )

    def test_no_piece_is_larger_than_a_document_or_a_broken_character(self) -> None:
        """A cut inside a character would store bytes nothing can decode."""

        store, transport = self.store({COMMIT_URL: (200, b"{}")})
        store.save("あ" * MAX_DOCUMENT_BYTES)
        for write in self.written(transport):
            piece = write["update"]["fields"]["document"]["stringValue"]
            self.assertLessEqual(len(piece.encode()), MAX_DOCUMENT_BYTES)
            self.assertGreater(len(piece), 0)

    def test_a_split_text_is_read_back_whole_and_in_order(self) -> None:
        store, transport = self.store(self.chain("one", "two", "three"))
        self.assertEqual(store.load(), "onetwothree")
        self.assertEqual(len(transport.requests), 3)

    def test_a_part_the_head_counts_but_nothing_holds_stops_the_start(self) -> None:
        """Text that stops early is not the text this wrote."""

        replies = self.chain("one", "two")
        replies[f"{DOCUMENT_URL}~1"] = (404, b"")
        store, _ = self.store(replies)
        with self.assertRaises(GcpUnavailable):
            store.load()

    def test_shrunk_text_drops_the_parts_it_no_longer_fills(self) -> None:
        replies = self.chain("one", "two", "three")
        replies[COMMIT_URL] = (200, b"{}")
        store, transport = self.store(replies)
        store.load()
        store.save("small")
        self.assertEqual(
            [write for write in self.written(transport) if "delete" in write],
            [
                {"delete": f"{DATABASE}/documents/state/channel_data~1"},
                {"delete": f"{DATABASE}/documents/state/channel_data~2"},
            ],
        )


class FakeFirestore:
    """Enough of the database to answer what the store writes into it."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}

    def __call__(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        if method == "GET":
            held = self.documents.get(url[len(FIRESTORE_ROOT) + 1 :])
            if held is None:
                return 404, b""
            return 200, json.dumps({"fields": held}).encode()
        for write in json.loads(body or b"")["writes"]:
            if "delete" in write:
                self.documents.pop(write["delete"], None)
            else:
                self.documents[write["update"]["name"]] = write["update"]["fields"]
        return 200, b"{}"


class FirestoreRoundTripTests(unittest.TestCase):
    """What a restart reads has to be what the process before it wrote."""

    def store(self, database: FakeFirestore) -> FirestoreStateStore:
        return FirestoreStateStore(DATABASE, "channel_data", token, transport=database)

    def big(self) -> str:
        document = json.dumps({"titles": ["視聴者の名前" * 12] * 4_000})
        self.assertGreater(len(document.encode()), MAX_DOCUMENT_BYTES)
        return document

    def test_a_module_larger_than_a_document_reads_back_exactly(self) -> None:
        database = FakeFirestore()
        document = self.big()
        self.store(database).save(document)
        self.assertGreater(len(database.documents), 1)
        self.assertEqual(self.store(database).load(), document)

    def test_a_module_that_shrinks_leaves_no_stale_part_to_read(self) -> None:
        database = FakeFirestore()
        store = self.store(database)
        store.save(self.big())
        store.save('{"titles":[]}')
        self.assertEqual(len(database.documents), 1)
        self.assertEqual(self.store(database).load(), '{"titles":[]}')


class DurabilityTests(unittest.TestCase):
    def test_nothing_configured_keeps_everything_in_memory(self) -> None:
        keep = durability_from_env({})
        self.assertIsNone(keep.store("channel_data"))
        self.assertIsNone(keep.credential_store())

    def test_persisting_without_a_vault_stops_the_start(self) -> None:
        """Otherwise the deployment looks durable and drops every credential."""

        for environment in (
            {"YNA_FIRESTORE_DATABASE": DATABASE},
            {"YNA_STATE_DIR": "/state"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(RuntimeError):
                    durability_from_env(environment)

    def test_firestore_wins_over_a_directory(self) -> None:
        keep = durability_from_env(
            {
                "YNA_FIRESTORE_DATABASE": DATABASE,
                "YNA_STATE_DIR": "/state",
                "YNA_CREDENTIAL_SECRET": SECRET,
            }
        )
        self.assertIsInstance(keep.store("channel_data"), FirestoreStateStore)

    def test_the_named_secret_is_the_credential_store(self) -> None:
        keep = durability_from_env(
            {"YNA_FIRESTORE_DATABASE": DATABASE, "YNA_CREDENTIAL_SECRET": SECRET}
        )
        self.assertIsInstance(keep.credential_store(), SecretManagerStateStore)


class CredentialWiringTests(unittest.TestCase):
    """The credential document goes to the vault or nowhere, never to a module.

    A module store holds session digests and who may reach which workspace, and
    a deployment may keep those on a plain disk. A refresh token wired into that
    same store would be a token written in the clear, which is the failure this
    whole arrangement exists to prevent.
    """

    def test_no_token_is_written_beside_the_module_documents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            services = build_services(
                "https://app.example",
                google=CONFIG,
                durability=Durability(directory=directory),
            )
            services.connections._vault.put("ws-1", "slot-1", credential("rt-secret"))
            written = "".join(
                path.read_text(encoding="utf-8")
                for path in directory.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("rt-secret", written)


class ScheduledCallerTests(unittest.TestCase):
    """The door the scheduler knocks on, and everyone else knocks on too."""

    ACCOUNT = "drain@yna.iam.gserviceaccount.com"
    AUDIENCE = "https://yna.example/internal/drain"

    def claims(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "iss": "https://accounts.google.com",
            "email": self.ACCOUNT,
            "email_verified": "true",
            "aud": self.AUDIENCE,
            "exp": str(int(time.time()) + 300),
        }
        payload.update(overrides)
        return payload

    def caller(
        self, claims: dict[str, object], status: int = 200
    ) -> ScheduledCaller:
        self.transport = FakeTransport(
            {TOKENINFO_URL: (status, json.dumps(claims).encode())}
        )
        return ScheduledCaller(
            self.ACCOUNT, self.AUDIENCE, transport=self.transport
        )

    def test_a_token_from_the_configured_job_is_accepted(self) -> None:
        self.assertTrue(self.caller(self.claims()).verify("Bearer id-token"))

    def test_the_token_is_never_put_in_a_url(self) -> None:
        """A query string ends up in logs; the drain's key must not."""

        self.caller(self.claims()).verify("Bearer id-token")

        method, url, _, body = self.transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertNotIn("id-token", url)
        self.assertIn(b"id_token=id-token", body or b"")

    def test_a_token_minted_for_another_service_is_refused(self) -> None:
        """The audience is what stops a token being replayed at us."""

        caller = self.caller(self.claims(aud="https://elsewhere.example/drain"))

        self.assertFalse(caller.verify("Bearer id-token"))

    def test_a_token_from_another_account_is_refused(self) -> None:
        caller = self.caller(self.claims(email="someone@example.com"))

        self.assertFalse(caller.verify("Bearer id-token"))

    def test_an_unverified_address_is_refused(self) -> None:
        caller = self.caller(self.claims(email_verified="false"))

        self.assertFalse(caller.verify("Bearer id-token"))

    def test_an_expired_token_is_refused(self) -> None:
        caller = self.caller(self.claims(exp=str(int(time.time()) - 1)))

        self.assertFalse(caller.verify("Bearer id-token"))

    def test_a_missing_expiry_is_refused_rather_than_ignored(self) -> None:
        claims = self.claims()
        del claims["exp"]

        self.assertFalse(self.caller(claims).verify("Bearer id-token"))

    def test_a_header_that_is_not_a_bearer_token_never_reaches_google(self) -> None:
        caller = self.caller(self.claims())

        for header in (None, "", "id-token", "Basic id-token", "Bearer "):
            self.assertFalse(caller.verify(header), header)

        self.assertEqual(self.transport.requests, [])

    def test_google_refusing_the_token_refuses_the_caller(self) -> None:
        caller = self.caller({"error": "invalid_token"}, status=400)

        self.assertFalse(caller.verify("Bearer id-token"))

    def test_google_being_unreachable_refuses_the_caller(self) -> None:
        """An outage must close the door, not hold it open."""

        def unreachable(*_args: object, **_kwargs: object) -> tuple[int, bytes]:
            raise GcpUnavailable("transport failed")

        caller = ScheduledCaller(
            self.ACCOUNT, self.AUDIENCE, transport=unreachable
        )

        self.assertFalse(caller.verify("Bearer id-token"))
