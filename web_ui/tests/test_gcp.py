from __future__ import annotations

import base64
import json
import unittest

from web_ui.container import durability_from_env
from web_ui.gcp import (
    KMS_ROOT,
    GcpUnavailable,
    GcsStateStore,
    KmsEnvelope,
    MetadataToken,
)
from web_ui.tests.test_google_provider import FakeTransport


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


class GcsStateStoreTests(unittest.TestCase):
    BUCKET = "https://storage.googleapis.com"

    def store(self, replies: dict[str, tuple[int, bytes]]):
        transport = FakeTransport(replies)
        return GcsStateStore("yna-state", "channel_data.json", token,
                             transport=transport), transport

    def test_a_missing_object_reads_as_nothing_rather_than_failing(self) -> None:
        store, _ = self.store({self.BUCKET: (404, b"")})
        self.assertIsNone(store.load())

    def test_it_returns_the_object_text(self) -> None:
        store, _ = self.store({self.BUCKET: (200, "{}".encode())})
        self.assertEqual(store.load(), "{}")

    def test_an_unreadable_bucket_stops_instead_of_starting_empty(self) -> None:
        """Starting empty would invite owners to re-authorize a live grant."""

        store, _ = self.store({self.BUCKET: (503, b"")})
        with self.assertRaises(GcpUnavailable):
            store.load()

    def test_saving_sends_the_document_as_the_body(self) -> None:
        store, transport = self.store({self.BUCKET: (200, b"{}")})
        store.save('{"a":1}')
        method, url, headers, body = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertIn("uploadType=media", url)
        self.assertEqual(body, b'{"a":1}')
        self.assertEqual(headers["Authorization"], "Bearer sa-token")

    def test_a_refused_write_is_raised(self) -> None:
        store, _ = self.store({self.BUCKET: (403, b"")})
        with self.assertRaises(GcpUnavailable):
            store.save("{}")


class KmsEnvelopeTests(unittest.TestCase):
    KEY = "projects/p/locations/global/keyRings/r/cryptoKeys/k"

    def test_it_sends_base64_and_returns_the_sealed_text(self) -> None:
        sealed = base64.b64encode(b"sealed-bytes").decode()
        transport = FakeTransport(
            {KMS_ROOT: (200, json.dumps({"ciphertext": sealed}).encode())}
        )
        envelope = KmsEnvelope(self.KEY, token, transport=transport)
        self.assertEqual(envelope.encrypt(b"plain"), sealed)
        _, url, _, body = transport.requests[0]
        self.assertTrue(url.endswith(":encrypt"))
        self.assertEqual(
            json.loads(body)["plaintext"], base64.b64encode(b"plain").decode()
        )

    def test_decrypt_returns_the_bytes_back(self) -> None:
        plain = base64.b64encode(b"plain").decode()
        transport = FakeTransport(
            {KMS_ROOT: (200, json.dumps({"plaintext": plain}).encode())}
        )
        envelope = KmsEnvelope(self.KEY, token, transport=transport)
        sealed = base64.b64encode(b"sealed-bytes").decode()
        self.assertEqual(envelope.decrypt(sealed), b"plain")
        _, url, _, _ = transport.requests[0]
        self.assertTrue(url.endswith(":decrypt"))

    def test_a_refusal_never_reaches_the_transport_body(self) -> None:
        transport = FakeTransport({KMS_ROOT: (403, b"key denied for caller")})
        envelope = KmsEnvelope(self.KEY, token, transport=transport)
        with self.assertRaises(GcpUnavailable) as raised:
            envelope.encrypt(b"plain")
        self.assertNotIn("caller", str(raised.exception))

    def test_a_document_that_is_not_base64_is_refused_before_any_call(self) -> None:
        transport = FakeTransport({})
        envelope = KmsEnvelope(self.KEY, token, transport=transport)
        with self.assertRaises(GcpUnavailable):
            envelope.decrypt("this is not base64!!")
        self.assertEqual(transport.requests, [])


class DurabilityTests(unittest.TestCase):
    def test_nothing_configured_keeps_everything_in_memory(self) -> None:
        keep = durability_from_env({})
        self.assertIsNone(keep.store("channel_data"))
        self.assertIsNone(keep.envelope())

    def test_persisting_without_a_key_stops_the_start(self) -> None:
        """Otherwise the deployment looks durable and drops every credential."""

        for environment in (
            {"YNA_STATE_BUCKET": "yna-state"},
            {"YNA_STATE_DIR": "/state"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(RuntimeError):
                    durability_from_env(environment)

    def test_a_bucket_wins_over_a_directory(self) -> None:
        keep = durability_from_env(
            {"YNA_STATE_BUCKET": "b", "YNA_STATE_DIR": "/state", "YNA_KMS_KEY": "k"}
        )
        self.assertIsInstance(keep.store("channel_data"), GcsStateStore)
