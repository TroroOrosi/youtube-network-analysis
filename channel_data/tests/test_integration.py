from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from channel_data.models import (
    AuthorCommentActivity,
    CommentCoverage,
    CoverageLimitation,
    SilentAnalysisDataset,
    SubscriberRegistryEntry,
    VideoCoverageScope,
)
from subscriber_analytics.analytics_core import (
    AnalysisRequest,
    CommentActivity,
    LimitationCode,
    Segment,
    SubscriberRecord,
    analyze,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class ChannelDataAnalyticsIntegrationTests(unittest.TestCase):
    def test_ready_dataset_maps_losslessly_to_all_four_segments(self) -> None:
        registry = tuple(
            SubscriberRegistryEntry(
                workspace_id="workspace-1",
                channel_id="channel-1",
                subscriber_channel_id=channel_id,
                title=channel_id,
                api_published_at=subscribed_at,
                first_seen_at=subscribed_at,
                last_seen_at=NOW,
                observation_count=1,
                last_snapshot_id="snapshot-1",
            )
            for channel_id, subscribed_at in (
                ("new-silent", NOW - timedelta(days=10)),
                ("old-silent", NOW - timedelta(days=200)),
                ("dormant", NOW - timedelta(days=200)),
                ("active", NOW - timedelta(days=200)),
            )
        )
        dataset = SilentAnalysisDataset(
            subscriber_registry=registry,
            author_activity=(
                AuthorCommentActivity("dormant", 2, NOW - timedelta(days=100)),
                AuthorCommentActivity("active", 3, NOW - timedelta(days=10)),
            ),
            snapshot_id="snapshot-1",
            snapshot_captured_at=NOW,
            inventory_id="inventory-1",
            inventory_captured_at=NOW,
            subscriber_limitations=(
                CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
                CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
            ),
            comment_coverage=CommentCoverage(
                "inventory-1",
                VideoCoverageScope.OWNER_VIDEOS,
                2,
                2,
                0,
                NOW,
                True,
            ),
        )

        result = analyze(
            subscribers=tuple(
                SubscriberRecord(
                    channel_id=entry.subscriber_channel_id,
                    title=entry.title,
                    api_published_at=entry.api_published_at,
                    first_seen_at=entry.first_seen_at,
                    last_seen_at=entry.last_seen_at,
                )
                for entry in dataset.subscriber_registry
            ),
            comment_activity=tuple(
                CommentActivity(
                    author_channel_id=row.author_channel_id,
                    comment_count=row.comment_count,
                    last_comment_at=row.last_comment_at,
                )
                for row in dataset.author_activity
            ),
            request=AnalysisRequest(reference_time=NOW),
        )

        self.assertEqual(
            {row.channel_id: row.segment for row in result.rows},
            {
                "new-silent": Segment.NEW_SILENT,
                "old-silent": Segment.OLD_SILENT,
                "dormant": Segment.DORMANT,
                "active": Segment.ACTIVE,
            },
        )
        self.assertEqual(result.limitations, (LimitationCode.PUBLIC_SUBSCRIPTIONS_ONLY,))
        self.assertEqual(
            set(dataset.subscriber_limitations),
            {
                CoverageLimitation.PUBLIC_SUBSCRIPTIONS_ONLY,
                CoverageLimitation.PROVIDER_RESULT_CAP_POSSIBLE,
            },
        )
        self.assertTrue(dataset.comment_coverage.is_complete)
        self.assertIs(
            dataset.comment_coverage.coverage_scope,
            VideoCoverageScope.OWNER_VIDEOS,
        )


if __name__ == "__main__":
    unittest.main()
