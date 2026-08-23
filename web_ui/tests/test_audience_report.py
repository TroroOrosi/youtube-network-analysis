from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
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

    def test_empty_analysis_families_are_valid(self) -> None:
        from web_ui.audience_report import load_audience_report

        document = valid_document()
        for name in (
            "top_channels",
            "viewer_breadth",
            "categories",
            "communities",
            "network_relationships",
            "affinity",
            "popularity_affinity",
        ):
            document[name] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            report = load_audience_report(path)

        self.assertEqual(report.top_channels, ())
        self.assertEqual(report.viewer_breadth, ())
        self.assertEqual(report.popularity_affinity, ())

    def test_summary_counts_must_be_positive(self) -> None:
        from web_ui.audience_report import load_audience_report

        for name in valid_document()["summary"]:  # type: ignore[union-attr]
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                document = valid_document()
                document["summary"][name] = 0  # type: ignore[index]
                path = Path(directory) / "report.json"
                path.write_text(json.dumps(document), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "must be positive"):
                    load_audience_report(path)

    def test_missing_and_malformed_json_fail_closed(self) -> None:
        from web_ui.audience_report import load_audience_report

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                load_audience_report(missing)

            malformed = Path(directory) / "malformed.json"
            malformed.write_text("{not json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_audience_report(malformed)

    def test_missing_and_malformed_workbook_fail_closed(self) -> None:
        from web_ui.audience_report import validate_audience_report_workbook

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.xlsx"
            with self.assertRaises(FileNotFoundError):
                validate_audience_report_workbook(missing)

            malformed = Path(directory) / "malformed.xlsx"
            malformed.write_bytes(b"not an xlsx")
            with self.assertRaisesRegex(ValueError, "valid Excel workbook"):
                validate_audience_report_workbook(malformed)

            incomplete = Path(directory) / "incomplete.xlsx"
            with zipfile.ZipFile(incomplete, "w") as workbook:
                workbook.writestr("[Content_Types].xml", "<Types />")
                workbook.writestr("_rels/.rels", "<Relationships />")
                workbook.writestr("xl/workbook.xml", "<workbook />")
            with self.assertRaisesRegex(ValueError, "valid Excel workbook"):
                validate_audience_report_workbook(incomplete)

if __name__ == "__main__":
    unittest.main()
