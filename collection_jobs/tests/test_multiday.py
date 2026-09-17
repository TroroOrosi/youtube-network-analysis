"""Daily-budget continuations use real services and synthetic provider pages."""
from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from channel_connections.models import SubscriberRow, VideoRow
from channel_data import snapshot as data_snapshot
from channel_data.service import ChannelDataService
from collection_jobs import snapshot
from collection_jobs.models import CancelRun, EnqueueRun, ExecuteRun, RunFailureReason, RunKind, RunStatus
from collection_jobs.service import CollectionJobsService
from collection_jobs.tests.support import NOW, build_stack, context
from collection_jobs.tests.test_persistence import FakeStore


class MultiDayTests(unittest.TestCase):
    def setUp(self):
        self.stack = build_stack(page_size=50)
        self.owner = context()
        self.connection = self.stack.connect(self.owner)
        self.jobs_store, self.data_store = FakeStore(), FakeStore()
        self.budget = 10000
        self.restart()

    def restart(self):
        self.stack.channel_data = ChannelDataService(clock=self.stack.clock, state_store=self.data_store)
        self.jobs = CollectionJobsService(
            clock=self.stack.clock, tokens=self.stack.jobs._tokens,
            broker=self.stack.connections, targets=self.stack.connections,
            channel_data=self.stack.channel_data, page_size=50,
            daily_quota_units=self.budget, state_store=self.jobs_store,
        )

    def enqueue(self, kind, key):
        return self.jobs.enqueue_run(self.owner, EnqueueRun(
            connection_id=self.connection.connection_id, kind=kind, idempotency_key=key))

    def execute(self, run, key):
        return self.jobs.execute_run(self.owner, ExecuteRun(run_id=run.run_id, idempotency_key=key))

    def test_2051_subscribers_span_five_days_keep_1000_accepted_until_complete(self):
        self.stack.data_gateway.subscribers = tuple(
            SubscriberRow(f'UC_sub_{i}', f'Subscriber {i}', NOW) for i in range(1000))
        self.assertEqual(self.execute(self.enqueue(RunKind.SUBSCRIBERS, 'original'), 'original').status,
                         RunStatus.SUCCEEDED)
        previous = data_snapshot.load(self.data_store.document).accepted_subscriber_snapshot.copy()
        self.stack.data_gateway.calls.clear()
        self.stack.clock.advance(timedelta(days=1))
        self.budget = 30  # Ten synthetic pages/day, each costs three units.
        self.restart()
        self.stack.data_gateway.subscribers += tuple(
            SubscriberRow(f'UC_sub_{i}', f'Subscriber {i}', NOW) for i in range(1000, 2051))
        run = self.enqueue(RunKind.SUBSCRIBERS, 'larger')
        for day in range(5):
            result = self.execute(run, f'day-{day}')
            self.assertEqual(result.attempt, 1)
            self.assertEqual(result.run_id, run.run_id)
            state = data_snapshot.load(self.data_store.document)
            if day < 4:
                self.assertEqual(result.status, RunStatus.QUEUED)
                self.assertEqual(result.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
                self.assertIsNone(result.finished_at)
                self.assertEqual(state.accepted_subscriber_snapshot, previous)
                self.assertEqual(len(state.subscriber_registry), 1000)
                before = len(self.stack.data_gateway.calls)
                self.assertEqual(self.jobs.execute_due_runs(self.owner, self.stack.clock.now(), 10), ())
                self.assertEqual(len(self.stack.data_gateway.calls), before)
                self.stack.clock.advance(timedelta(days=1))
                self.restart()
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.pages_fetched, 42)
        self.assertEqual(result.quota_spent, 126)
        self.assertEqual(len(state.subscriber_registry), 2051)
        self.assertEqual([c.page_token for c in self.stack.data_gateway.calls],
                         [None] + [f'page-{i}' for i in range(50, 2051, 50)])
        self.assertFalse(snapshot.load(self.jobs_store.document).page_checkpoints)

    def test_video_inventory_resumes_across_days_before_comments(self):
        self.budget = 6
        self.stack.data_gateway.videos = tuple(VideoRow(f'v{i}', f'Video {i}', NOW) for i in range(151))
        self.stack.data_gateway.comment_authors = {}
        self.restart()
        run = self.enqueue(RunKind.OWNER_CONTENT, 'videos')
        first = self.execute(run, 'first')
        self.assertEqual(first.status, RunStatus.QUEUED)
        self.assertEqual(first.pages_fetched, 2)
        self.assertFalse(data_snapshot.load(self.data_store.document).accepted_video_inventory)
        self.stack.clock.advance(timedelta(days=1))
        self.restart()
        second = self.execute(run, 'second')
        self.assertEqual(second.status, RunStatus.QUEUED)
        self.assertEqual(second.pages_fetched, 4)
        self.assertTrue(data_snapshot.load(self.data_store.document).accepted_video_inventory)
        self.assertFalse(data_snapshot.load(self.data_store.document).comment_coverage)
        self.stack.clock.advance(timedelta(days=1))
        self.budget = 1000
        self.restart()
        final = self.execute(run, 'third')
        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual([c.page_token for c in self.stack.data_gateway.calls if c.operation == 'LIST_VIDEOS'],
                         [None, 'page-50', 'page-100', 'page-150'])
        self.assertEqual(final.pages_fetched, 155)
        self.assertEqual(final.quota_spent, 465)

    def test_cancelled_daily_wait_clears_checkpoint_and_never_collects_again(self):
        self.budget = 3
        self.restart()
        self.stack.data_gateway.subscribers = tuple(SubscriberRow(f'UC_s{i}', f'S{i}', NOW) for i in range(101))
        run = self.enqueue(RunKind.SUBSCRIBERS, 'cancel')
        self.assertEqual(self.execute(run, 'first').status, RunStatus.QUEUED)
        self.jobs.cancel_run(self.owner, CancelRun(run_id=run.run_id, idempotency_key='cancel'))
        self.stack.clock.advance(timedelta(days=1))
        self.restart()
        self.assertFalse(snapshot.load(self.jobs_store.document).page_checkpoints)
        self.assertEqual(self.jobs.execute_due_runs(self.owner, self.stack.clock.now(), 10), ())
        self.assertEqual(len(self.stack.data_gateway.calls), 1)

    def test_empty_daily_budget_retains_queue_for_more_than_three_days(self):
        self.budget = 0
        self.restart()
        run = self.enqueue(RunKind.OWNER_CONTENT, 'empty')
        for day in range(5):
            result = self.execute(run, f'day-{day}')
            self.assertEqual(result.status, RunStatus.QUEUED)
            self.assertEqual(result.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
            self.assertIsNotNone(result.next_attempt_at)
            self.stack.clock.advance(timedelta(days=1))
            self.restart()
        self.assertEqual(self.stack.data_gateway.calls, [])
        self.budget = 10000
        self.restart()
        self.assertEqual(self.execute(run, 'restored-budget').status, RunStatus.SUCCEEDED)

    def test_provider_daily_exhaustion_preserves_each_phase_and_cost_after_restart(self):
        from channel_connections.ports import ProviderQuotaExceeded
        scenarios = (
            (RunKind.SUBSCRIBERS, 'list_subscribers', 'LIST_SUBSCRIBERS'),
            (RunKind.OWNER_CONTENT, 'list_videos', 'LIST_VIDEOS'),
            (RunKind.OWNER_CONTENT, 'list_video_comment_authors', 'LIST_VIDEO_COMMENT_AUTHORS'),
        )
        for kind, method, operation in scenarios:
            with self.subTest(phase=operation):
                self.setUp()
                self.stack.clock.advance(datetime(2026, 9, 17, 0, 5, tzinfo=UTC) - self.stack.clock.now())
                self.stack.data_gateway.subscribers = tuple(SubscriberRow(f'UC_s{i}', f'S{i}', NOW) for i in range(101))
                self.stack.data_gateway.videos = tuple(VideoRow(f'v{i}', f'Video {i}', NOW) for i in range(101))
                original = getattr(self.stack.data_gateway, method)
                attempts = []
                def exhausted(*args, **kwargs):
                    attempts.append(kwargs['page_token'])
                    if len(attempts) == 2:
                        raise ProviderQuotaExceeded(quota_cost=1)
                    return original(*args, **kwargs)
                run = self.enqueue(kind, 'quota')
                with patch.object(self.stack.data_gateway, method, exhausted):
                    first = self.execute(run, 'first')
                    self.assertEqual(first.status, RunStatus.QUEUED)
                    self.assertEqual(first.attempt, 1)
                    self.assertEqual(first.failure_reason, RunFailureReason.QUOTA_EXHAUSTED)
                    self.assertEqual(first.next_attempt_at, datetime(2026, 9, 17, 7, tzinfo=UTC))
                    saved_spent = first.quota_spent
                    self.assertGreater(saved_spent, 0)
                    self.restart()
                    self.assertEqual(self.jobs.execute_due_runs(self.owner, self.stack.clock.now(), 10), ())
                    self.assertEqual(len(attempts), 2)
                    self.stack.clock.advance(first.next_attempt_at - self.stack.clock.now())
                    self.restart()
                    final = self.execute(run, 'after-reset')
                self.assertEqual(final.status, RunStatus.SUCCEEDED)
                self.assertEqual(attempts[1], attempts[2])  # The rejected page, not page one.
                self.assertGreaterEqual(final.quota_spent, saved_spent)
                self.assertEqual(final.quota_spent, sum(c.quota_cost if hasattr(c, 'quota_cost') else 3
                                                      for c in self.stack.data_gateway.calls) + 1)
                self.assertEqual(self.stack.connections.get_connection(self.owner, self.connection.connection_id).status.value,
                                 'ACTIVE')

    def test_reauthorization_after_daily_wait_does_not_erase_spent_progress(self):
        from channel_connections.models import ReportCredentialInvalidation
        self.budget = 3
        self.restart()
        self.stack.data_gateway.subscribers = tuple(
            SubscriberRow(f'UC_s{i}', f'S{i}', NOW) for i in range(101))
        run = self.enqueue(RunKind.SUBSCRIBERS, 'reauth')
        paused = self.execute(run, 'first')
        self.assertEqual(paused.pages_fetched, 1)
        self.stack.connections.report_credential_invalidation(self.owner,
            ReportCredentialInvalidation(connection_id=self.connection.connection_id,
                                         idempotency_key='revoke'))
        self.stack.clock.advance(timedelta(days=1))
        self.restart()
        failed = self.execute(run, 'after-reset')
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(failed.failure_reason, RunFailureReason.REAUTH_REQUIRED)
        self.assertEqual(failed.pages_fetched, paused.pages_fetched)
        self.assertEqual(failed.quota_spent, paused.quota_spent)
        self.assertFalse(snapshot.load(self.jobs_store.document).page_checkpoints)

    def test_provider_reset_is_pacific_midnight_with_dst_not_utc_midnight(self):
        from collection_jobs.service import _next_youtube_day
        cases = (
            ('2026-09-17T00:05:00+00:00', '2026-09-17T07:00:00+00:00'),
            ('2026-01-17T00:05:00+00:00', '2026-01-17T08:00:00+00:00'),
            ('2026-03-08T08:00:00+00:00', '2026-03-09T07:00:00+00:00'),
            ('2026-11-01T07:00:00+00:00', '2026-11-02T08:00:00+00:00'),
        )
        for before, after in cases:
            with self.subTest(before=before):
                self.assertEqual(_next_youtube_day(datetime.fromisoformat(before)), datetime.fromisoformat(after))
