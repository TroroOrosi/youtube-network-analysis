from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from channel_data.errors import ChannelDataError
from channel_data.memory import MemoryState
from channel_data.models import (
    CollectionFailureCode,
    CollectionHistoryQuery,
    CollectionKind,
    CollectionStatus,
    CollectionState,
    FinishCollection,
    PageRequest,
    StartCollection,
)
from channel_data.service import ChannelDataService
from workspace_access.models import Permission, Role, WorkspaceContext


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def context(*permissions: Permission) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="workspace-1",
        user_id="user-1",
        membership_id="membership-1",
        role=Role.OWNER,
        permissions=frozenset(permissions),
        session_id="session-1",
        authorization_revision=1,
        resolved_at=NOW,
    )


class CollectionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ChannelDataService()
        self.writer = context(Permission.COLLECTION_RUN)
        self.reader = context(Permission.COLLECTION_READ)

    def start(
        self,
        collection_id: str,
        kind: CollectionKind = CollectionKind.SUBSCRIBERS,
        started_at: datetime = NOW,
    ):
        return self.service.start_collection(
            self.writer,
            StartCollection(
                "channel-1",
                collection_id,
                kind,
                started_at,
                f"start-{collection_id}",
            ),
        )

    def test_partial_and_failed_transitions_are_terminal_and_replay_safely(self) -> None:
        self.start("partial")
        partial_command = FinishCollection(
            "channel-1",
            "partial",
            CollectionStatus.PARTIAL,
            NOW + timedelta(minutes=1),
            2,
            3,
            None,
            "finish-partial",
        )
        partial = self.service.finish_collection(self.writer, partial_command)
        self.assertIs(self.service.finish_collection(self.writer, partial_command), partial)
        self.assertEqual(partial.progress_current, 2)
        self.assertIsNone(partial.accepted_generation_id)

        with self.assertRaises(ChannelDataError) as caught:
            self.service.finish_collection(
                self.writer,
                FinishCollection(
                    "channel-1",
                    "partial",
                    CollectionStatus.FAILED,
                    NOW + timedelta(minutes=2),
                    2,
                    3,
                    CollectionFailureCode.UNEXPECTED_FAILURE,
                    "different-finish-intent",
                ),
            )
        self.assertEqual(caught.exception.code, "INVALID_COLLECTION_TRANSITION")

        self.start("failed")
        failed = self.service.finish_collection(
            self.writer,
            FinishCollection(
                "channel-1",
                "failed",
                CollectionStatus.FAILED,
                NOW + timedelta(minutes=1),
                0,
                2,
                CollectionFailureCode.QUOTA_EXHAUSTED,
                "finish-failed",
            ),
        )
        self.assertIs(failed.failure_code, CollectionFailureCode.QUOTA_EXHAUSTED)

    def test_complete_without_exact_candidate_fails_atomically(self) -> None:
        original = self.start("complete-without-candidate")
        with self.assertRaises(ChannelDataError) as caught:
            self.service.finish_collection(
                self.writer,
                FinishCollection(
                    "channel-1",
                    "complete-without-candidate",
                    CollectionStatus.COMPLETE,
                    NOW + timedelta(minutes=1),
                    1,
                    1,
                    None,
                    "finish-complete",
                ),
            )
        self.assertEqual(caught.exception.code, "INVALID_COLLECTION_TRANSITION")

        history = self.service.list_collection_history(
            self.reader,
            CollectionHistoryQuery("channel-1"),
        )
        self.assertEqual(history.items, (original,))

    def test_freshness_and_history_require_read_permission_and_order_deterministically(self) -> None:
        older = self.start("z-older", started_at=NOW - timedelta(hours=1))
        newer_b = self.start("b-newer")
        newer_a = self.start("a-newer")
        self.service.finish_collection(
            self.writer,
            FinishCollection(
                "channel-1",
                "a-newer",
                CollectionStatus.PARTIAL,
                NOW + timedelta(minutes=1),
                0,
                1,
                None,
                "finish-a",
            ),
        )

        with self.assertRaises(ChannelDataError) as caught:
            self.service.get_freshness(self.writer, "channel-1")
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")

        history = self.service.list_collection_history(
            self.reader,
            CollectionHistoryQuery("channel-1"),
            PageRequest(limit=10),
        )
        self.assertEqual(
            [state.collection_id for state in history.items],
            ["a-newer", "b-newer", "z-older"],
        )
        freshness = self.service.get_freshness(self.reader, "channel-1")
        self.assertEqual(freshness.subscribers.latest_attempt.collection_id, "a-newer")
        self.assertIsNone(freshness.subscribers.latest_accepted_success)
        self.assertIn(older, history.items)
        self.assertIn(newer_a.collection_id, {item.collection_id for item in history.items})
        self.assertIn(newer_b.collection_id, {item.collection_id for item in history.items})

    def test_concurrent_exact_finish_has_one_terminal_result(self) -> None:
        self.start("concurrent")
        command = FinishCollection(
            "channel-1",
            "concurrent",
            CollectionStatus.PARTIAL,
            NOW + timedelta(minutes=1),
            1,
            2,
            None,
            "finish-concurrent",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service.finish_collection(self.writer, command), range(2)))
        self.assertIs(results[0], results[1])

    def test_failed_latest_attempt_does_not_erase_latest_accepted_success(self) -> None:
        accepted = CollectionState(
            "accepted",
            "workspace-1",
            "channel-1",
            CollectionKind.SUBSCRIBERS,
            CollectionStatus.COMPLETE,
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            1,
            1,
            None,
            "snapshot-1",
        )
        failed = CollectionState(
            "failed-later",
            "workspace-1",
            "channel-1",
            CollectionKind.SUBSCRIBERS,
            CollectionStatus.FAILED,
            NOW,
            NOW + timedelta(minutes=1),
            0,
            1,
            CollectionFailureCode.QUOTA_EXHAUSTED,
            None,
        )
        state = MemoryState(
            collections={
                ("workspace-1", accepted.collection_id): accepted,
                ("workspace-1", failed.collection_id): failed,
            }
        )
        service = ChannelDataService(state)

        freshness = service.get_freshness(self.reader, "channel-1")
        self.assertIs(freshness.subscribers.latest_attempt, failed)
        self.assertIs(freshness.subscribers.latest_accepted_success, accepted)

    def test_history_cursor_is_query_bound_and_preserves_a_captured_result(self) -> None:
        for index in range(5):
            self.start(
                f"collection-{index}",
                started_at=NOW - timedelta(minutes=index),
            )
        query = CollectionHistoryQuery("channel-1")
        first = self.service.list_collection_history(
            self.reader,
            query,
            PageRequest(limit=2),
        )
        self.assertIsNotNone(first.next_cursor)
        self.assertNotIn("workspace-1", first.next_cursor)
        self.assertNotIn("channel-1", first.next_cursor)

        self.start("collection-new-after-page", started_at=NOW + timedelta(minutes=1))
        second = self.service.list_collection_history(
            self.reader,
            query,
            PageRequest(cursor=first.next_cursor, limit=2),
        )
        third = self.service.list_collection_history(
            self.reader,
            query,
            PageRequest(cursor=second.next_cursor, limit=2),
        )
        traversed = first.items + second.items + third.items
        self.assertEqual(len(traversed), 5)
        self.assertEqual(len({item.collection_id for item in traversed}), 5)
        self.assertNotIn("collection-new-after-page", {item.collection_id for item in traversed})
        self.assertIsNone(third.next_cursor)

        for invalid_page, invalid_query in (
            (PageRequest(cursor="tampered", limit=2), query),
            (
                PageRequest(cursor=first.next_cursor, limit=2),
                CollectionHistoryQuery("channel-1", CollectionKind.VIDEOS),
            ),
        ):
            with self.subTest(query=invalid_query.kind):
                with self.assertRaises(ChannelDataError) as caught:
                    self.service.list_collection_history(
                        self.reader,
                        invalid_query,
                        invalid_page,
                    )
                self.assertEqual(caught.exception.code, "INVALID_CURSOR")


if __name__ == "__main__":
    unittest.main()
