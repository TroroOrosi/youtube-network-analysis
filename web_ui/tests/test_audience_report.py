from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


def valid_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "視聴者ネットワーク分析",
        "source_label": "匿名化済み既存調査データ（デモ）",
        "coverage_period": "2026-08-01〜2026-08-20",
        "methodology": "公開購読情報を集計した参考分析です。",
        "summary": {
            "viewers": 808,
            "channels": 106_568,
            "viewer_channel_edges": 235_024,
            "network_nodes": 14_694,
            "network_edges": 226_516,
            "communities": 50,
            "median_breadth": 162,
        },
        "top_channels": [
            {"channel": "学びチャンネル", "viewers": 265, "panel_share": 0.328}
        ],
        "viewer_breadth": [
            {"label": "1〜49", "viewers": 120, "share": 0.1485}
        ],
        "categories": [
            {
                "category": "自己啓発・学び",
                "channels": 16,
                "viewer_links": 1_743,
                "share": 0.0838,
            }
        ],
        "communities": [
            {
                "rank": 1,
                "channels": 3_015,
                "edges": 20_809,
                "internal_weight": 4_476.99,
                "representatives": "代表A / 代表B",
            }
        ],
        "network_relationships": [
            {
                "left": "チャンネルA",
                "right": "チャンネルB",
                "shared_viewers": 80,
                "weighted_cosine": 0.91,
            }
        ],
        "affinity": [
            {
                "channel": "関連チャンネル",
                "observed_viewers": 17,
                "panel_share": 0.021,
                "affinity_lift": 180.96,
            }
        ],
        "popularity_affinity": [
            {
                "channel": "関連チャンネル",
                "popularity_rank": 120,
                "affinity_rank": 1,
                "rank_gap": 119,
                "panel_share": 0.021,
                "affinity_lift": 180.96,
            }
        ],
    }


class AudienceReportTests(unittest.TestCase):
    def test_a_valid_document_becomes_an_immutable_report(self) -> None:
        from web_ui.audience_report import load_audience_report

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(valid_document()), encoding="utf-8")

            report = load_audience_report(path)

        self.assertEqual(report.summary.viewers, 808)
        self.assertEqual(report.top_channels[0].channel, "学びチャンネル")
        with self.assertRaises((AttributeError, TypeError)):
            report.summary.viewers = 1  # type: ignore[misc]

    def test_a_document_with_raw_identifiers_is_rejected(self) -> None:
        from web_ui.audience_report import load_audience_report

        document = valid_document()
        document["viewer_ids"] = ["viewer_001"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unexpected report fields"):
                load_audience_report(path)

if __name__ == "__main__":
    unittest.main()
