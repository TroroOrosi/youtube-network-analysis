"""Large-channel regressions: real domain services, synthetic provider pages."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from channel_connections.models import CommentAuthorRow
from channel_data.models import CollectionHistoryQuery, CollectionKind
from collection_jobs import snapshot
from collection_jobs.models import EnqueueRun, ExecuteRun, RunKind, RunStatus
from collection_jobs.service import CollectionJobsService
from collection_jobs.tests.support import NOW, build_stack, context
from collection_jobs.tests.test_persistence import FakeStore


class LargeChannelTests(unittest.TestCase):
    def setUp(self):
        self.stack = build_stack(page_size=2)
        self.owner = context()
        self.connection = self.stack.connect(self.owner)
        self.store = FakeStore()
        self.jobs = self.restart()

    def restart(self):
        self.jobs = CollectionJobsService(
            clock=self.stack.clock, tokens=self.stack.jobs._tokens,
            broker=self.stack.connections, targets=self.stack.connections,
            channel_data=self.stack.channel_data, page_size=2,
            daily_quota_units=self.stack.jobs._daily_quota_units,
            state_store=self.store,
        )
        return self.jobs

    def enqueue(self, kind=RunKind.OWNER_CONTENT, key="large"):
        return self.jobs.enqueue_run(self.owner, EnqueueRun(
            connection_id=self.connection.connection_id, kind=kind,
            idempotency_key=key,
        ))

    def execute(self, run, key="slice", seconds=None):
        return self.jobs.execute_run(self.owner, ExecuteRun(
            run_id=run.run_id, idempotency_key=key, slice_seconds=seconds,
        ))

    def one_busy_video(self, count=12):
        self.stack.data_gateway.videos = self.stack.data_gateway.videos[:1]
        self.stack.data_gateway.comment_authors["video-1"] = tuple(
            CommentAuthorRow("video-1", f"UC_author_{i}", 1, NOW)
            for i in range(count)
        )

    def slow_down(self, method):
        original = getattr(self.stack.data_gateway, method)
        def fetch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.stack.clock.advance(timedelta(seconds=2))
            return result
        return patch.object(self.stack.data_gateway, method, fetch)

    def test_repeated_author_across_pages_is_merged_not_a_failed_collection(self):
        self.one_busy_video(3)
        self.stack.data_gateway.comment_authors["video-1"] = (
            CommentAuthorRow("video-1", "UC_sub_1", 2, NOW - timedelta(days=3)),
            CommentAuthorRow("video-1", "UC_sub_2", 1, NOW),
            CommentAuthorRow("video-1", "UC_sub_1", 4, NOW),
        )
        self.execute(self.enqueue(RunKind.SUBSCRIBERS, "subs"), "subs")
        result = self.execute(self.enqueue())
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        dataset = self.stack.channel_data.load_silent_analysis_dataset(self.owner, "UC_channel_1")
        authors = {row.author_channel_id: row for row in dataset.author_activity}
        self.assertEqual(authors["UC_sub_1"].comment_count, 6)
        self.assertEqual(authors["UC_sub_1"].last_comment_at, NOW)
        self.assertEqual(len(authors), 2)

    def test_single_video_yields_between_pages_and_resumes_after_restart(self):
        self.one_busy_video()
        run = self.enqueue()
        with self.slow_down("list_video_comment_authors"):
            first = self.execute(run, "first", 3)
            self.assertEqual(first.status, RunStatus.QUEUED)
            self.assertLessEqual(len(self.stack.data_gateway.calls), 3)  # inventory + two pages
            with self.assertRaises(Exception) as raised:
                self.stack.channel_data.load_silent_analysis_dataset(self.owner, "UC_channel_1")
            self.assertEqual(raised.exception.code, "DATASET_NOT_READY")
            for index in range(10):
                self.restart()
                finished = self.execute(run, f"next-{index}", 3)
                if finished.status is not RunStatus.QUEUED:
                    break
        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        comments = [call for call in self.stack.data_gateway.calls if call.operation == "LIST_VIDEO_COMMENT_AUTHORS"]
        self.assertEqual([call.page_token for call in comments], [None, "page-2", "page-4", "page-6", "page-8", "page-10"])
        self.assertEqual(finished.pages_fetched, 7)
        self.assertEqual(finished.quota_spent, 21)
        self.restart()
        self.assertEqual(self.jobs.get_run(self.owner, run.run_id).status, RunStatus.SUCCEEDED)

    def test_single_video_can_span_more_than_three_daily_budgets(self):
        self.one_busy_video(20)
        self.stack.jobs._daily_quota_units = 6
        self.restart()
        run = self.enqueue()
        for day in range(12):
            result = self.execute(run, f"day-{day}")
            if result.status is not RunStatus.QUEUED:
                break
            self.stack.clock.advance(timedelta(days=1))
            self.restart()
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertGreater(day, 3)
        comments = [call for call in self.stack.data_gateway.calls if call.operation == "LIST_VIDEO_COMMENT_AUTHORS"]
        self.assertEqual([call.page_token for call in comments], [None] + [f"page-{i}" for i in range(2, 20, 2)])
        self.assertEqual(result.quota_spent, 33)
        self.assertEqual(result.pages_fetched, 11)

    def test_subscriber_pagination_obeys_slice_deadline(self):
        with self.slow_down("list_subscribers"):
            run = self.enqueue(RunKind.SUBSCRIBERS)
            first = self.execute(run, "first", 1)
            self.assertEqual(first.status, RunStatus.QUEUED)
            self.assertEqual(len(self.stack.data_gateway.calls), 1)
            for index in range(5):
                self.restart()
                last = self.execute(run, f"rest-{index}", 1)
                if last.status is not RunStatus.QUEUED:
                    break
        self.assertEqual(last.status, RunStatus.SUCCEEDED)
        self.assertEqual(last.pages_fetched, 3)
        self.assertEqual(len(self.stack.data_gateway.calls), 3)
        history = self.stack.channel_data.list_collection_history(self.owner, CollectionHistoryQuery(channel_id="UC_channel_1"))
        self.assertEqual(len([row for row in history.items if row.kind is CollectionKind.SUBSCRIBERS]), 1)

    def test_inventory_pagination_obeys_slice_deadline(self):
        with self.slow_down("list_videos"):
            run = self.enqueue()
            first = self.execute(run, "first", 1)
            self.assertEqual(first.status, RunStatus.QUEUED)
            self.assertEqual(len(self.stack.data_gateway.calls), 1)
            self.restart()
            last = self.execute(run, "next")
        self.assertEqual(last.status, RunStatus.SUCCEEDED)
        videos = [call for call in self.stack.data_gateway.calls if call.operation == "LIST_VIDEOS"]
        self.assertEqual([call.page_token for call in videos], [None, "page-2"])

    def test_publication_exception_leaves_a_retryable_failed_run_not_running(self):
        run = self.enqueue()
        with patch.object(self.stack.channel_data, "publish_video_inventory", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.execute(run)
        self.restart()
        self.assertEqual(self.jobs.get_run(self.owner, run.run_id).status, RunStatus.FAILED)
        self.assertEqual(self.enqueue(key="retry").status, RunStatus.QUEUED)

    def test_previously_stranded_running_run_is_failed_closed_on_start(self):
        run = self.enqueue()
        state = snapshot.load(self.store.document)
        key = next(iter(state.runs))
        state.runs[key] = replace(run, status=RunStatus.RUNNING, started_at=NOW)
        self.store.document = snapshot.dump(state)
        self.restart()
        restored = self.jobs.get_run(self.owner, run.run_id)
        self.assertEqual(restored.status, RunStatus.FAILED)
        self.assertIsNotNone(restored.finished_at)
        self.assertEqual(self.enqueue(key="retry").status, RunStatus.QUEUED)

    def test_cancel_clears_private_page_checkpoint_rows(self):
        from collection_jobs.models import CancelRun
        self.one_busy_video()
        run = self.enqueue()
        with self.slow_down("list_video_comment_authors"):
            self.execute(run, seconds=1)
        self.assertIn("UC_author_0", self.store.document)
        self.jobs.cancel_run(self.owner, CancelRun(run_id=run.run_id, idempotency_key="cancel"))
        self.restart()
        self.assertNotIn("UC_author_0", self.store.document)
        self.assertFalse(self.jobs._state.page_checkpoints)

    def test_version_one_documents_migrate_without_dropping_existing_runs(self):
        run = self.enqueue()
        import json
        document = json.loads(self.store.document)
        document["version"] = 1
        document["state"]["f"].pop("page_checkpoints")
        self.store.document = json.dumps(document)
        self.restart()
        self.assertEqual(self.jobs.get_run(self.owner, run.run_id).status, RunStatus.QUEUED)
        self.assertEqual(self.execute(run).status, RunStatus.SUCCEEDED)

    def test_failure_still_reports_pages_and_quota_already_consumed(self):
        run = self.enqueue()
        with patch.object(self.stack.channel_data, "publish_video_inventory", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.execute(run)
        result = self.jobs.get_run(self.owner, run.run_id)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(result.quota_spent, 6)

    def test_repeated_provider_cursor_stops_without_publishing_incomplete_data(self):
        original = self.stack.data_gateway.list_subscribers
        def looping(*args, **kwargs):
            return replace(original(*args, **kwargs), next_page_token="page-2")
        run = self.enqueue(RunKind.SUBSCRIBERS)
        with patch.object(self.stack.data_gateway, "list_subscribers", looping):
            result = self.execute(run)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(len(self.stack.data_gateway.calls), 2)
        self.assertFalse(self.jobs._state.page_checkpoints)

    def test_twelve_hundred_videos_finish_with_exact_counts_across_slices(self):
        from channel_connections.models import VideoRow
        count = 1200
        self.stack.data_gateway.videos = tuple(
            VideoRow(f"large-video-{i}", f"Video {i}", NOW) for i in range(count)
        )
        self.stack.data_gateway.comment_authors = {
            video.video_id: (CommentAuthorRow(video.video_id, "UC_sub_1", 1, NOW),)
            for video in self.stack.data_gateway.videos
        }
        self.jobs._page_size = 50
        self.execute(self.enqueue(RunKind.SUBSCRIBERS, "subs"), "subs")
        run = self.enqueue()
        # Each request advances simulated time, without waiting in the test.
        original = self.stack.data_gateway.list_video_comment_authors
        def fetch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.stack.clock.advance(timedelta(milliseconds=100))
            return result
        with patch.object(self.stack.data_gateway, "list_video_comment_authors", fetch):
            for index in range(100):
                result = self.execute(run, f"batch-{index}", 2)
                if result.status is not RunStatus.QUEUED:
                    break
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertGreater(index, 1)
        self.assertEqual(result.pages_fetched, count + 24)
        self.assertEqual(result.quota_spent, (count + 24) * 3)
        dataset = self.stack.channel_data.load_silent_analysis_dataset(self.owner, "UC_channel_1")
        author = next(row for row in dataset.author_activity if row.author_channel_id == "UC_sub_1")
        self.assertEqual(author.comment_count, count)
