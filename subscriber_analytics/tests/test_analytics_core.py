from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone

from subscriber_analytics.analytics_core import (
    AnalysisFilters,
    AnalysisRequest,
    AnalysisValidationError,
    CommentActivity,
    LimitationCode,
    Segment,
    SegmentPolicy,
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


class AnalyticsCoreFiltersAndValidationTests(unittest.TestCase):
    def subscriber(
        self,
        channel_id: str,
        subscribed_at: datetime,
        *,
        last_seen_at: datetime = REFERENCE_TIME,
    ) -> SubscriberRecord:
        return SubscriberRecord(
            channel_id=channel_id,
            title=channel_id,
            api_published_at=None,
            first_seen_at=subscribed_at,
            last_seen_at=last_seen_at,
        )

    def analyze_with_filters(
        self,
        subscribers: list[SubscriberRecord],
        activity: list[CommentActivity] | None = None,
        filters: AnalysisFilters | None = None,
        policy: SegmentPolicy | None = None,
    ):
        return analyze(
            subscribers,
            activity or [],
            AnalysisRequest(
                reference_time=REFERENCE_TIME,
                filters=filters or AnalysisFilters(),
                segment_policy=policy or SegmentPolicy(),
            ),
        )

    def test_segment_cutoffs_are_inclusive(self):
        one_microsecond = timedelta(microseconds=1)
        subscribers = [
            self.subscriber("UC_NEW_BOUNDARY", REFERENCE_TIME - timedelta(days=90)),
            self.subscriber(
                "UC_OLD_OUTSIDE",
                REFERENCE_TIME - timedelta(days=90) - one_microsecond,
            ),
            self.subscriber("UC_ACTIVE_BOUNDARY", REFERENCE_TIME - timedelta(days=200)),
            self.subscriber("UC_DORMANT_OUTSIDE", REFERENCE_TIME - timedelta(days=200)),
        ]
        activity = [
            CommentActivity(
                "UC_ACTIVE_BOUNDARY",
                1,
                REFERENCE_TIME - timedelta(days=90),
            ),
            CommentActivity(
                "UC_DORMANT_OUTSIDE",
                1,
                REFERENCE_TIME - timedelta(days=90) - one_microsecond,
            ),
        ]

        result = self.analyze_with_filters(subscribers, activity)

        self.assertEqual(
            {row.channel_id: row.segment for row in result.rows},
            {
                "UC_NEW_BOUNDARY": Segment.NEW_SILENT,
                "UC_OLD_OUTSIDE": Segment.OLD_SILENT,
                "UC_ACTIVE_BOUNDARY": Segment.ACTIVE,
                "UC_DORMANT_OUTSIDE": Segment.DORMANT,
            },
        )

    def test_subscription_filters_use_inclusive_utc_boundaries_and_and_semantics(self):
        subscribers = [
            self.subscriber("UC_BEFORE", datetime(2026, 7, 31, 23, 59, tzinfo=UTC)),
            self.subscriber("UC_START", datetime(2026, 8, 1, 0, 0, tzinfo=UTC)),
            self.subscriber("UC_CUTOFF", datetime(2026, 8, 1, 12, 0, tzinfo=UTC)),
            self.subscriber("UC_MIDDLE", datetime(2026, 8, 2, 12, 0, tzinfo=UTC)),
            self.subscriber(
                "UC_END",
                datetime(2026, 8, 2, 23, 59, 59, 999999, tzinfo=UTC),
            ),
            self.subscriber("UC_AFTER", datetime(2026, 8, 3, 0, 0, tzinfo=UTC)),
        ]

        within = self.analyze_with_filters(
            subscribers,
            filters=AnalysisFilters(subscribed_within=timedelta(days=19)),
        )
        since = self.analyze_with_filters(
            subscribers,
            filters=AnalysisFilters(subscribed_since=date(2026, 8, 1)),
        )
        until = self.analyze_with_filters(
            subscribers,
            filters=AnalysisFilters(subscribed_until=date(2026, 8, 2)),
        )
        combined = self.analyze_with_filters(
            subscribers,
            filters=AnalysisFilters(
                subscribed_within=timedelta(days=19),
                subscribed_since=date(2026, 8, 1),
                subscribed_until=date(2026, 8, 2),
                never_commented=True,
            ),
        )

        self.assertEqual(
            {row.channel_id for row in within.rows},
            {"UC_CUTOFF", "UC_MIDDLE", "UC_END", "UC_AFTER"},
        )
        self.assertNotIn("UC_BEFORE", {row.channel_id for row in since.rows})
        self.assertIn("UC_START", {row.channel_id for row in since.rows})
        self.assertIn("UC_END", {row.channel_id for row in until.rows})
        self.assertNotIn("UC_AFTER", {row.channel_id for row in until.rows})
        self.assertEqual(
            {row.channel_id for row in combined.rows},
            {"UC_CUTOFF", "UC_MIDDLE", "UC_END"},
        )
        self.assertEqual(combined.scope_count, 6)
        self.assertEqual(combined.filtered_count, 3)

    def test_comment_filters_distinguish_never_old_boundary_and_recent_activity(self):
        subscribers = [
            self.subscriber("UC_NEVER", REFERENCE_TIME - timedelta(days=200)),
            self.subscriber("UC_OLD_COMMENT", REFERENCE_TIME - timedelta(days=200)),
            self.subscriber("UC_BOUNDARY", REFERENCE_TIME - timedelta(days=200)),
            self.subscriber("UC_RECENT", REFERENCE_TIME - timedelta(days=200)),
        ]
        activity = [
            CommentActivity(
                "UC_OLD_COMMENT", 2, REFERENCE_TIME - timedelta(days=91)
            ),
            CommentActivity("UC_BOUNDARY", 1, REFERENCE_TIME - timedelta(days=90)),
            CommentActivity("UC_RECENT", 3, REFERENCE_TIME - timedelta(days=10)),
        ]

        never = self.analyze_with_filters(
            subscribers,
            activity,
            AnalysisFilters(never_commented=True),
        )
        no_comment_within = self.analyze_with_filters(
            subscribers,
            activity,
            AnalysisFilters(no_comment_within=timedelta(days=90)),
        )

        self.assertEqual([row.channel_id for row in never.rows], ["UC_NEVER"])
        self.assertEqual(
            {row.channel_id for row in no_comment_within.rows},
            {"UC_NEVER", "UC_OLD_COMMENT"},
        )

    def test_segment_filter_is_applied_after_classification(self):
        subscribers = [
            self.subscriber("UC_NEW", REFERENCE_TIME - timedelta(days=10)),
            self.subscriber("UC_OLD", REFERENCE_TIME - timedelta(days=100)),
            self.subscriber("UC_DORMANT", REFERENCE_TIME - timedelta(days=10)),
        ]
        activity = [
            CommentActivity(
                "UC_DORMANT", 1, REFERENCE_TIME - timedelta(days=100)
            )
        ]

        result = self.analyze_with_filters(
            subscribers,
            activity,
            AnalysisFilters(segments=frozenset({Segment.NEW_SILENT, Segment.DORMANT})),
        )

        self.assertEqual(
            {row.channel_id for row in result.rows},
            {"UC_NEW", "UC_DORMANT"},
        )
        self.assertEqual(result.scope_silent_count, 2)
        self.assertEqual(result.filtered_silent_count, 1)

    def test_latest_observation_scope_can_be_explicitly_expanded(self):
        subscribers = [
            self.subscriber("UC_LATEST", REFERENCE_TIME - timedelta(days=5)),
            self.subscriber(
                "UC_EARLIER",
                REFERENCE_TIME - timedelta(days=10),
                last_seen_at=REFERENCE_TIME - timedelta(hours=1),
            ),
        ]

        default_result = self.analyze_with_filters(subscribers)
        expanded_result = self.analyze_with_filters(
            subscribers,
            filters=AnalysisFilters(include_not_seen_latest=True),
        )

        self.assertEqual([row.channel_id for row in default_result.rows], ["UC_LATEST"])
        self.assertEqual(default_result.excluded_not_seen_latest_count, 1)
        self.assertEqual(
            {row.channel_id for row in expanded_result.rows},
            {"UC_LATEST", "UC_EARLIER"},
        )
        self.assertEqual(expanded_result.scope_count, 2)
        self.assertEqual(expanded_result.excluded_not_seen_latest_count, 0)

    def test_empty_input_and_non_subscriber_activity_are_safe(self):
        empty_result = self.analyze_with_filters([])
        result = self.analyze_with_filters(
            [self.subscriber("UC_SUBSCRIBER", REFERENCE_TIME - timedelta(days=100))],
            [
                CommentActivity(
                    "UC_OTHER", 5, REFERENCE_TIME - timedelta(days=1)
                )
            ],
        )

        self.assertEqual(empty_result.rows, ())
        self.assertEqual(empty_result.scope_count, 0)
        self.assertEqual(empty_result.filtered_count, 0)
        self.assertEqual(result.rows[0].comment_count, 0)
        self.assertEqual(result.rows[0].segment, Segment.OLD_SILENT)

    def test_equivalent_permutations_produce_identical_tie_broken_results(self):
        subscribers = [
            self.subscriber("UC_B", REFERENCE_TIME - timedelta(days=20)),
            self.subscriber("UC_A", REFERENCE_TIME - timedelta(days=20)),
            self.subscriber("UC_C", REFERENCE_TIME - timedelta(days=30)),
        ]
        activity = [
            CommentActivity("UC_A", 1, REFERENCE_TIME - timedelta(days=1)),
            CommentActivity("UC_C", 1, REFERENCE_TIME - timedelta(days=100)),
        ]

        forward = self.analyze_with_filters(subscribers, activity)
        reversed_input = self.analyze_with_filters(
            list(reversed(subscribers)), list(reversed(activity))
        )

        self.assertEqual(forward, reversed_input)
        self.assertEqual(
            [row.channel_id for row in forward.rows],
            ["UC_A", "UC_B", "UC_C"],
        )

    def test_timezone_aware_inputs_are_normalized_to_utc(self):
        japan = timezone(timedelta(hours=9))
        local_reference = REFERENCE_TIME.astimezone(japan)
        subscriber = SubscriberRecord(
            channel_id="UC_JST",
            title="jst",
            api_published_at=None,
            first_seen_at=(REFERENCE_TIME - timedelta(days=1)).astimezone(japan),
            last_seen_at=local_reference,
        )

        result = analyze(
            [subscriber],
            [],
            AnalysisRequest(reference_time=local_reference),
        )

        self.assertEqual(result.reference_time, REFERENCE_TIME)
        self.assertEqual(result.rows[0].first_seen_at.tzinfo, UTC)
        self.assertEqual(
            result.rows[0].first_seen_at,
            REFERENCE_TIME - timedelta(days=1),
        )

    def assert_validation_error(
        self,
        expected_code: str,
        expected_field: str,
        subscribers: list[SubscriberRecord],
        activity: list[CommentActivity] | None = None,
        request: AnalysisRequest | None = None,
    ) -> None:
        with self.assertRaises(AnalysisValidationError) as raised:
            analyze(
                subscribers,
                activity or [],
                request or AnalysisRequest(reference_time=REFERENCE_TIME),
            )
        self.assertEqual(raised.exception.code, expected_code)
        self.assertEqual(raised.exception.field, expected_field)
        self.assertTrue(raised.exception.message)

    def test_channel_ids_must_be_non_empty_and_unique(self):
        valid = self.subscriber("UC1", REFERENCE_TIME)
        self.assert_validation_error(
            "EMPTY_CHANNEL_ID",
            "subscribers[0].channel_id",
            [self.subscriber("  ", REFERENCE_TIME)],
        )
        self.assert_validation_error(
            "DUPLICATE_SUBSCRIBER",
            "subscribers[1].channel_id",
            [valid, valid],
        )
        self.assert_validation_error(
            "EMPTY_CHANNEL_ID",
            "comment_activity[0].author_channel_id",
            [valid],
            [CommentActivity("", 0, None)],
        )
        self.assert_validation_error(
            "DUPLICATE_COMMENT_ACTIVITY",
            "comment_activity[1].author_channel_id",
            [valid],
            [CommentActivity("UC_OTHER", 0, None), CommentActivity("UC_OTHER", 0, None)],
        )

    def test_comment_counts_and_timestamps_must_be_consistent(self):
        subscriber = self.subscriber("UC1", REFERENCE_TIME)
        for invalid_count in (-1, 1.5, True):
            with self.subTest(invalid_count=invalid_count):
                self.assert_validation_error(
                    "INVALID_COMMENT_COUNT",
                    "comment_activity[0].comment_count",
                    [subscriber],
                    [CommentActivity("UC1", invalid_count, None)],
                )
        self.assert_validation_error(
            "INCONSISTENT_COMMENT_ACTIVITY",
            "comment_activity[0].last_comment_at",
            [subscriber],
            [CommentActivity("UC1", 1, None)],
        )
        self.assert_validation_error(
            "INCONSISTENT_COMMENT_ACTIVITY",
            "comment_activity[0].last_comment_at",
            [subscriber],
            [CommentActivity("UC1", 0, REFERENCE_TIME)],
        )

    def test_all_datetime_inputs_must_be_timezone_aware(self):
        aware = self.subscriber("UC1", REFERENCE_TIME)
        naive = REFERENCE_TIME.replace(tzinfo=None)
        cases = [
            (
                "request.reference_time",
                [aware],
                [],
                AnalysisRequest(reference_time=naive),
            ),
            (
                "subscribers[0].api_published_at",
                [SubscriberRecord("UC1", "one", naive, REFERENCE_TIME, REFERENCE_TIME)],
                [],
                None,
            ),
            (
                "subscribers[0].first_seen_at",
                [SubscriberRecord("UC1", "one", None, naive, REFERENCE_TIME)],
                [],
                None,
            ),
            (
                "subscribers[0].last_seen_at",
                [SubscriberRecord("UC1", "one", None, REFERENCE_TIME, naive)],
                [],
                None,
            ),
            (
                "comment_activity[0].last_comment_at",
                [aware],
                [CommentActivity("UC1", 1, naive)],
                None,
            ),
        ]
        for expected_field, subscribers, activity, request in cases:
            with self.subTest(field=expected_field):
                self.assert_validation_error(
                    "NAIVE_DATETIME",
                    expected_field,
                    subscribers,
                    activity,
                    request,
                )

    def test_windows_must_be_positive_and_date_range_ordered(self):
        subscriber = self.subscriber("UC1", REFERENCE_TIME)
        requests = [
            (
                "request.segment_policy.recent_subscriber_window",
                AnalysisRequest(
                    REFERENCE_TIME,
                    segment_policy=SegmentPolicy(
                        recent_subscriber_window=timedelta(0)
                    ),
                ),
            ),
            (
                "request.segment_policy.recent_activity_window",
                AnalysisRequest(
                    REFERENCE_TIME,
                    segment_policy=SegmentPolicy(
                        recent_activity_window=timedelta(days=-1)
                    ),
                ),
            ),
            (
                "request.filters.subscribed_within",
                AnalysisRequest(
                    REFERENCE_TIME,
                    filters=AnalysisFilters(subscribed_within=timedelta(0)),
                ),
            ),
            (
                "request.filters.no_comment_within",
                AnalysisRequest(
                    REFERENCE_TIME,
                    filters=AnalysisFilters(no_comment_within=timedelta(days=-1)),
                ),
            ),
        ]
        for expected_field, request in requests:
            with self.subTest(field=expected_field):
                self.assert_validation_error(
                    "INVALID_TIME_WINDOW",
                    expected_field,
                    [subscriber],
                    request=request,
                )
        self.assert_validation_error(
            "INVALID_DATE_RANGE",
            "request.filters.subscribed_since",
            [subscriber],
            request=AnalysisRequest(
                REFERENCE_TIME,
                filters=AnalysisFilters(
                    subscribed_since=date(2026, 8, 3),
                    subscribed_until=date(2026, 8, 2),
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
