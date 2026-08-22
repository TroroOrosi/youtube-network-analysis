"""The Google Cloud adapters: the identity, the vault, the module documents."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from web_ui.container import Durability, build_services, durability_from_env
from web_ui.gcp import (
    FIRESTORE_ROOT,
    SECRET_MANAGER_ROOT,
    FirestoreStateStore,
    GcpUnavailable,
    MetadataToken,
    SecretManagerStateStore,
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


def version_list(*numbers: int) -> tuple[int, bytes]:
    versions = [{"name": f"{SECRET}/versions/{number}"} for number in numbers]
    return 200, json.dumps({"versions": versions}).encode()


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


class FirestoreStateStoreTests(unittest.TestCase):
    def store(self, replies: dict[str, tuple[int, bytes]]):
        transport = FakeTransport(replies)
        return (
            FirestoreStateStore(DATABASE, "channel_data", token, transport=transport),
            transport,
        )

    def held(self, document: str) -> tuple[int, bytes]:
        fields = {"fields": {"document": {"stringValue": document}}}
        return 200, json.dumps(fields).encode()

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
        store, transport = self.store({DOCUMENT_URL: (200, b"{}")})
        store.save('{"a":1}')
        method, url, headers, body = transport.requests[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, DOCUMENT_URL)
        self.assertEqual(headers["Authorization"], "Bearer sa-token")
        self.assertEqual(
            json.loads(body or b""),
            {"fields": {"document": {"stringValue": '{"a":1}'}}},
        )

    def test_a_refused_write_is_raised(self) -> None:
        store, _ = self.store({DOCUMENT_URL: (403, b"")})
        with self.assertRaises(GcpUnavailable):
            store.save("{}")


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
