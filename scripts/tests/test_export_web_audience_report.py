from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from scripts.export_web_audience_report import build_document, write_artifacts


class ExportWebAudienceReportTests(unittest.TestCase):
    def test_generated_snapshot_reconciles_every_analysis_family(self) -> None:
        document = build_document()

        self.assertEqual(document["summary"]["viewers"], 808)
        self.assertEqual(document["summary"]["channels"], 106_568)
        self.assertEqual(document["summary"]["viewer_channel_edges"], 235_024)
        for name in (
            "top_channels",
            "viewer_breadth",
            "categories",
            "communities",
            "network_relationships",
            "affinity",
            "popularity_affinity",
        ):
            self.assertGreater(len(document[name]), 0, name)

        serialized = json.dumps(document, ensure_ascii=False).lower()
        for forbidden in (
            '"viewer":',
            '"viewer_ids":',
            '"channel_id":',
            "viewer_001",
            "c:\\\\",
            "/users/",
            "token",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_web_and_excel_artifacts_share_headline_metrics(self) -> None:
        document = build_document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path = root / "report.json"
            workbook_path = root / "report.xlsx"

            write_artifacts(document, json_path, workbook_path)
            workbook = load_workbook(workbook_path, read_only=False, data_only=True)

        self.assertEqual(workbook.sheetnames, [
            "Summary",
            "Top Channels",
            "Viewer Breadth",
            "Interest Categories",
            "Communities",
            "Network Relationships",
            "Affinity",
            "Popularity vs Affinity",
            "Methodology",
        ])
        summary = workbook["Summary"]
        metrics = {summary.cell(row, 1).value: summary.cell(row, 2).value for row in range(6, 13)}
        self.assertEqual(metrics["視聴者数"], document["summary"]["viewers"])
        self.assertEqual(metrics["登録チャンネル数"], document["summary"]["channels"])
        self.assertEqual(len(workbook["Top Channels"]._charts), 1)
        self.assertEqual(len(workbook["Viewer Breadth"]._charts), 1)
        self.assertEqual(len(workbook["Interest Categories"]._charts), 1)

        summary = workbook["Summary"]
        self.assertTrue(summary.sheet_properties.pageSetUpPr.fitToPage)
        self.assertEqual(summary.page_setup.fitToWidth, 1)
        self.assertEqual(summary.page_setup.fitToHeight, 1)

        for name in workbook.sheetnames[1:-1]:
            sheet = workbook[name]
            self.assertEqual(sheet.page_setup.orientation, "landscape", name)
            self.assertTrue(sheet.sheet_properties.pageSetUpPr.fitToPage, name)
            self.assertEqual(sheet.page_setup.fitToWidth, 1, name)
            self.assertEqual(sheet.page_setup.fitToHeight, 0, name)
            self.assertEqual(sheet.print_title_rows, "$1:$3", name)

        methodology = workbook["Methodology"]
        self.assertEqual(methodology.page_setup.orientation, "landscape")
        self.assertTrue(methodology.sheet_properties.pageSetUpPr.fitToPage)
        self.assertEqual(methodology.page_setup.fitToWidth, 1)
        self.assertEqual(methodology.page_setup.fitToHeight, 1)


if __name__ == "__main__":
    unittest.main()
