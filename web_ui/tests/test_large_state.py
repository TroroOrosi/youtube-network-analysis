"""Exercise the real Firestore adapter against a size-limited REST double."""
from __future__ import annotations

import json
import unittest
from web_ui.gcp import FirestoreStateStore, GcpUnavailable
from web_ui.tests.test_gcp import DATABASE, FakeFirestore, token


class LimitedFirestore(FakeFirestore):
    def __init__(self):
        super().__init__()
        self.commit_sizes = []
        self.reject_commit = None

    def __call__(self, method, url, *, headers, body):
        if method == "POST":
            self.commit_sizes.append(len(body))
            if len(body) > 10 * 1024 * 1024:
                return 400, b'{}'
            if len(self.commit_sizes) == self.reject_commit:
                return 503, b'{}'
        return super().__call__(method, url, headers=headers, body=body)


class LargeStateTests(unittest.TestCase):
    def store(self, database):
        return FirestoreStateStore(DATABASE, "channel_data", token, transport=database)

    def large(self):
        # More than one Firestore request, including JSON escaping overhead.
        return json.dumps({"rows": ['日本語\\"' * 20] * 45000}, ensure_ascii=False)

    def test_state_bigger_than_one_commit_round_trips_without_large_requests(self):
        database = LimitedFirestore()
        text = self.large()
        self.assertGreater(len(text.encode()), 10 * 1024 * 1024)
        store = self.store(database)
        store.save(text)
        self.assertEqual(self.store(database).load(), text)
        self.assertGreater(len(database.commit_sizes), 1)
        self.assertLess(max(database.commit_sizes), 10 * 1024 * 1024)
        store.save("small again")
        self.assertEqual(self.store(database).load(), "small again")

    def test_failed_staging_never_replaces_the_previous_complete_state(self):
        database = LimitedFirestore()
        store = self.store(database)
        store.save('old state')
        database.reject_commit = 3  # old commit; first stage; refused later stage
        with self.assertRaises(GcpUnavailable):
            store.save(self.large())
        self.assertEqual(self.store(database).load(), 'old state')
        database.reject_commit = None
        store.save(self.large())
        self.assertEqual(self.store(database).load(), self.large())

    def test_failed_cleanup_does_not_report_a_committed_save_as_failed(self):
        database = LimitedFirestore()
        store = self.store(database)
        store.save(self.large())
        # Publish a new small head, then refuse deletion of its old chunks.
        database.reject_commit = len(database.commit_sizes) + 2
        store.save("replacement")
        self.assertEqual(self.store(database).load(), "replacement")

    def test_corrupt_large_state_is_never_loaded_as_partial_or_empty(self):
        database = LimitedFirestore()
        store = self.store(database)
        store.save(self.large())
        parts = [name for name in database.documents if not name.endswith("/channel_data")]
        self.assertTrue(parts)
        database.documents[parts[-1]]["document"]["stringValue"] = "tampered"
        with self.assertRaises(GcpUnavailable):
            self.store(database).load()

    def test_successful_replacement_reclaims_the_previous_generation(self):
        database = LimitedFirestore()
        store = self.store(database)
        store.save(self.large())
        old = set(database.documents)
        store.save(self.large() + " changed")
        self.assertTrue(old - set(database.documents))
        self.assertEqual(self.store(database).load(), self.large() + " changed")
        store.save("small")
        self.assertEqual(len(database.documents), 1)

    def test_lost_publish_response_is_confirmed_before_reporting_failure(self):
        class LostResponse(LimitedFirestore):
            def __call__(self, method, url, *, headers, body):
                result = super().__call__(method, url, headers=headers, body=body)
                if method == "POST" and any(
                    "generation" in write.get("update", {}).get("fields", {})
                    for write in json.loads(body)["writes"]
                ):
                    return 503, b'{}'  # committed, but its successful reply was lost
                return result
        database = LostResponse()
        store = self.store(database)
        store.save("before")
        store.save(self.large())
        self.assertEqual(self.store(database).load(), self.large())

    def test_reader_racing_generation_reclamation_retries_from_the_new_head(self):
        from web_ui.gcp import FIRESTORE_ROOT
        database = LimitedFirestore()
        writer = self.store(database)
        old_text = self.large()
        new_text = old_text + " new"
        writer.save(old_text)
        first_part = next(name for name in database.documents if not name.endswith("/channel_data"))
        triggered = False
        def transport(method, url, **kwargs):
            nonlocal triggered
            if method == "GET" and url == f"{FIRESTORE_ROOT}/{first_part}" and not triggered:
                triggered = True
                writer.save(new_text)
            return database(method, url, **kwargs)
        reader = FirestoreStateStore(DATABASE, "channel_data", token, transport=transport)
        self.assertEqual(reader.load(), new_text)
        self.assertTrue(triggered)

    def test_lost_small_snapshot_response_is_confirmed(self):
        class LostResponse(LimitedFirestore):
            def __call__(self, method, url, *, headers, body):
                result = super().__call__(method, url, headers=headers, body=body)
                if method == "POST":
                    raise GcpUnavailable("response lost after commit")
                return result
        database = LostResponse()
        self.store(database).save("small snapshot")
        self.assertEqual(self.store(database).load(), "small snapshot")
