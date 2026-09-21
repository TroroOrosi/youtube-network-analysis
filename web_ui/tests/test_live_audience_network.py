from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import UTC, datetime


class LiveAudienceReportTests(unittest.TestCase):
    def test_live_snapshot_builds_real_report_not_demo_artifact(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("web_ui.live_audience_network"))
        module = importlib.import_module("web_ui.live_audience_network")
        from collection_jobs import models as job_models

        channel = job_models.AudienceChannel("UC_a", "チャンネルA")
        shared = job_models.AudienceChannel("UC_shared", "共通チャンネル")
        snapshot = job_models.AudienceNetworkSnapshot(
            run_id="run_network",
            workspace_id="workspace-1",
            channel_id="UC_owner",
            captured_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
            viewers=(
                job_models.AudienceViewerSubscriptions(
                    viewer_channel_id="UC_viewer_1",
                    public=True,
                    subscriptions=(channel, shared),
                ),
                job_models.AudienceViewerSubscriptions(
                    viewer_channel_id="UC_viewer_2",
                    public=True,
                    subscriptions=(shared,),
                ),
                job_models.AudienceViewerSubscriptions(
                    viewer_channel_id="UC_private",
                    public=False,
                    subscriptions=(),
                ),
            ),
        )

        report = module.build_live_audience_report(snapshot)

        self.assertIn("ライブ", report.source_label)
        self.assertNotIn("デモ", report.source_label)
        self.assertEqual(report.summary.viewers, 2)
        self.assertEqual(report.summary.channels, 2)
        self.assertEqual(report.summary.viewer_channel_edges, 3)
        self.assertEqual(report.top_channels[0].channel, "共通チャンネル")
        self.assertEqual(report.top_channels[0].viewers, 2)


if __name__ == "__main__":
    unittest.main()
