from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

from subscriber_analytics.analytics_core import (
    AnalysisFilters,
    AnalysisRequest,
    CommentActivity,
    LimitationCode,
    Segment,
    SubscriberRecord,
    SubscriptionTimeSource,
    analyze,
)


REFERENCE_TIME = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class AnalyticsCoreDefaultAnalysisTests(unittest.TestCase):
    def test_default_analysis_classifies_all_segments_and_totals_silent_users(self):
        subscribers = [
            SubscriberRecord(
                channel_id="UC_NEW",
                title="new silent",
                api_published_at=REFERENCE_TIME - timedelta(days=30),
                first_seen_at=REFERENCE_TIME - timedelta(days=5),
                last_seen_at=REFERENCE_TIME,
            ),
            SubscriberRecord(
                channel_id="UC_OLD",
                title="old silent",
                api_published_at=None,
                first_seen_at=REFERENCE_TIME - timedelta(days=120),
                last_seen_at=REFERENCE_TIME,
            ),
            SubscriberRecord(
                channel_id="UC_DORMANT",
                title="dormant",
                api_published_at=None,
                first_seen_at=REFERENCE_TIME - timedelta(days=20),
                last_seen_at=REFERENCE_TIME,
            ),
            SubscriberRecord(
                channel_id="UC_ACTIVE",
                title="active",
                api_published_at=None,
                first_seen_at=REFERENCE_TIME - timedelta(days=200),
                last_seen_at=REFERENCE_TIME,
            ),
            SubscriberRecord(
                channel_id="UC_STALE",
                title="not seen in latest observation",
                api_published_at=None,
                first_seen_at=REFERENCE_TIME - timedelta(days=10),
                last_seen_at=REFERENCE_TIME - timedelta(days=1),
            ),
        ]
        comment_activity = [
            CommentActivity(
                author_channel_id="UC_DORMANT",
                comment_count=2,
                last_comment_at=REFERENCE_TIME - timedelta(days=91),
            ),
            CommentActivity(
                author_channel_id="UC_ACTIVE",
                comment_count=1,
                last_comment_at=REFERENCE_TIME - timedelta(days=90),
            ),
        ]
        original_subscribers = tuple(subscribers)
        original_activity = tuple(comment_activity)

        result = analyze(
            subscribers,
            comment_activity,
            AnalysisRequest(reference_time=REFERENCE_TIME),
        )

        self.assertEqual(
            [(row.channel_id, row.segment) for row in result.rows],
            [
                ("UC_DORMANT", Segment.DORMANT),
                ("UC_NEW", Segment.NEW_SILENT),
                ("UC_OLD", Segment.OLD_SILENT),
                ("UC_ACTIVE", Segment.ACTIVE),
            ],
        )
        self.assertEqual(result.scope_count, 4)
        self.assertEqual(result.filtered_count, 4)
        self.assertEqual(result.excluded_not_seen_latest_count, 1)
        self.assertEqual(result.scope_silent_count, 2)
        self.assertEqual(result.filtered_silent_count, 2)
        self.assertEqual(
            dict(result.scope_segment_counts),
            {
                Segment.NEW_SILENT: 1,
                Segment.OLD_SILENT: 1,
                Segment.DORMANT: 1,
                Segment.ACTIVE: 1,
            },
        )
        self.assertEqual(result.scope_segment_counts, result.filtered_segment_counts)
        self.assertEqual(result.reference_time, REFERENCE_TIME)
        self.assertEqual(result.filters, AnalysisFilters())
        self.assertEqual(
            result.limitations,
            (LimitationCode.PUBLIC_SUBSCRIPTIONS_ONLY,),
        )
        self.assertEqual(tuple(subscribers), original_subscribers)
        self.assertEqual(tuple(comment_activity), original_activity)

        new_silent = next(row for row in result.rows if row.channel_id == "UC_NEW")
        self.assertEqual(
            new_silent.subscribed_at,
            REFERENCE_TIME - timedelta(days=30),
        )
        self.assertEqual(
            new_silent.subscribed_at_source,
            SubscriptionTimeSource.API_PUBLISHED_AT,
        )
        old_silent = next(row for row in result.rows if row.channel_id == "UC_OLD")
        self.assertEqual(
            old_silent.subscribed_at_source,
            SubscriptionTimeSource.FIRST_SEEN_AT,
        )

    def test_results_and_summary_mappings_are_immutable(self):
        result = analyze(
            [
                SubscriberRecord(
                    channel_id="UC1",
                    title="one",
                    api_published_at=None,
                    first_seen_at=REFERENCE_TIME,
                    last_seen_at=REFERENCE_TIME,
                )
            ],
            [],
            AnalysisRequest(reference_time=REFERENCE_TIME),
        )

        with self.assertRaises(FrozenInstanceError):
            result.filtered_count = 0
        with self.assertRaises(TypeError):
            result.scope_segment_counts[Segment.NEW_SILENT] = 99


if __name__ == "__main__":
    unittest.main()
