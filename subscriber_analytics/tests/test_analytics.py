from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import collect_comments  # noqa: E402
import collect_subscribers  # noqa: E402
import common  # noqa: E402
import analytics_core  # noqa: E402
import extract_silent  # noqa: E402


class Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class Resource:
    def __init__(self, response):
        self.response = response

    def list(self, **_kwargs):
        return Request(self.response)

    def list_next(self, _request, _response):
        return None


class FakeYouTube:
    def __init__(self, videos):
        self._channels = Resource(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "PL1"}}}]}
        )
        self._playlist_items = Resource(
            {
                "items": [
                    {
                        "contentDetails": {"videoId": video_id},
                        "snippet": {"title": video_id},
                    }
                    for video_id in videos
                ]
            }
        )
        self._comment_threads = Resource({"items": []})

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._playlist_items

    def commentThreads(self):
        return self._comment_threads


class AnalyticsTests(unittest.TestCase):
    def write_analysis_fixture(self, data_dir: Path) -> None:
        registry = pd.DataFrame(
            [
                [
                    "UC_NEW",
                    "new silent",
                    "2026-07-21T12:00:00Z",
                    "2026-08-15T12:00:00Z",
                    "2026-08-20T12:00:00Z",
                    "1",
                ],
                [
                    "UC_OLD",
                    "old silent",
                    "",
                    "2026-04-22T12:00:00Z",
                    "2026-08-20T12:00:00Z",
                    "1",
                ],
                [
                    "UC_DORMANT",
                    "dormant",
                    "",
                    "2026-07-31T12:00:00Z",
                    "2026-08-20T12:00:00Z",
                    "1",
                ],
                [
                    "UC_ACTIVE",
                    "active",
                    "",
                    "2026-02-01T12:00:00Z",
                    "2026-08-20T12:00:00Z",
                    "1",
                ],
                [
                    "UC_STALE",
                    "not latest",
                    "",
                    "2026-08-10T12:00:00Z",
                    "2026-08-19T12:00:00Z",
                    "1",
                ],
            ],
            columns=collect_subscribers.REGISTRY_COLUMNS,
        )
        registry.to_csv(common.registry_path(data_dir), index=False)
        comments_dir = common.comments_dir(data_dir)
        comments_dir.mkdir(parents=True)
        pd.DataFrame(
            [
                ["UC_DORMANT", "2026-05-21T12:00:00Z"],
                ["UC_ACTIVE", "2026-08-10T12:00:00Z"],
            ],
            columns=["author_channel_id", "published_at"],
        ).to_csv(comments_dir / "video-1.csv", index=False)
        common.comment_state_path(data_dir).write_text(
            json.dumps(
                {
                    "status": "complete",
                    "coverage_scope": common.COMMENT_COVERAGE_OWNER,
                    "videos_listed": 1,
                    "videos_missing": 0,
                    "comment_coverage_complete": True,
                }
            ),
            encoding="utf-8",
        )

    def test_registry_preserves_first_seen_and_updates_last_seen(self):
        registry = pd.DataFrame(
            [["UC1", "old", "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "1"]],
            columns=collect_subscribers.REGISTRY_COLUMNS,
        )
        snapshot = pd.DataFrame(
            [["UC1", "new", "2025-12-01T00:00:00Z", "2026-02-01T00:00:00Z"]],
            columns=collect_subscribers.SNAPSHOT_COLUMNS,
        )
        result = collect_subscribers.update_registry(registry, snapshot).iloc[0]
        self.assertEqual(result["first_seen_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["last_seen_at"], "2026-02-01T00:00:00Z")
        self.assertEqual(result["snapshot_count"], 2)

    def test_full_comment_run_marks_dataset_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stats = collect_comments.run(FakeYouTube(["v1", "v2"]), "UC_TARGET", data_dir)
            state = json.loads(common.comment_state_path(data_dir).read_text(encoding="utf-8"))
            self.assertTrue(stats["comment_coverage_complete"])
            self.assertEqual(state["status"], "complete")
            self.assertTrue(state["comment_coverage_complete"])

    def test_public_only_comment_run_is_rejected_by_extractor(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            collect_comments.run(
                FakeYouTube(["v1"]),
                "UC_TARGET",
                data_dir,
                coverage_scope="public",
            )
            with self.assertRaises(SystemExit) as raised:
                extract_silent.validate_comment_collection(data_dir)
            self.assertIn("公開動画", str(raised.exception))

    def test_partial_comment_run_is_rejected_by_extractor(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            collect_comments.run(
                FakeYouTube(["v1", "v2"]), "UC_TARGET", data_dir, max_videos=1
            )
            with self.assertRaises(SystemExit):
                extract_silent.validate_comment_collection(data_dir)

    def test_split_comment_collection_is_accepted_after_all_videos_are_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            youtube = FakeYouTube(["v1", "v2"])
            collect_comments.run(youtube, "UC_TARGET", data_dir, max_videos=1)
            stats = collect_comments.run(youtube, "UC_TARGET", data_dir, max_videos=1)
            self.assertTrue(stats["comment_coverage_complete"])
            extract_silent.validate_comment_collection(data_dir)

    def test_missing_comment_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                extract_silent.validate_comment_collection(Path(tmp))

    def test_codespaces_secret_presence_is_reported_without_values(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                common.OAUTH_CLIENT_SECRET_ENV: '{"installed": {}}',
                common.OAUTH_TOKEN_ENV: '{"token": "secret"}',
                "YOUTUBE_API_KEY": "secret-key",
            },
            clear=False,
        ):
            data_dir = Path(tmp)
            status = common.oauth_config_status(
                data_dir / "client.json", data_dir / "token.json"
            )
            self.assertEqual(status["client_source"], common.OAUTH_CLIENT_SECRET_ENV)
            self.assertEqual(status["token_source"], common.OAUTH_TOKEN_ENV)
            self.assertTrue(status["api_key_present"])

    def test_comment_client_prefers_codespaces_oauth_token(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {common.OAUTH_TOKEN_ENV: '{"token": "secret"}'},
            clear=False,
        ), patch.object(
            common, "build_oauth_client", return_value="oauth-client"
        ) as oauth_builder, patch.object(
            common, "build_api_key_client", return_value="key-client"
        ) as key_builder:
            client = collect_comments.build_client(False, None, Path(tmp))
            self.assertEqual(client, "oauth-client")
            oauth_builder.assert_called_once()
            key_builder.assert_not_called()

    def test_comment_client_supports_explicit_api_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            common, "build_api_key_client", return_value="key-client"
        ) as key_builder:
            client = collect_comments.build_client(False, "api-key", Path(tmp))
            self.assertEqual(client, "key-client")
            key_builder.assert_called_once_with("api-key")

    def test_missing_oauth_channel_is_rejected(self):
        youtube = type(
            "YouTube",
            (),
            {"channels": lambda _self: Resource({"items": []})},
        )()
        with self.assertRaises(SystemExit):
            collect_subscribers.resolve_my_channel_id(youtube)

    def test_codespaces_never_starts_loopback_oauth(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "CODESPACES": "true",
                common.OAUTH_CLIENT_SECRET_ENV: "",
                common.OAUTH_TOKEN_ENV: "",
            },
            clear=False,
        ):
            data_dir = Path(tmp)
            with self.assertRaises(SystemExit) as raised:
                common.build_oauth_client(
                    client_secret=data_dir / "missing.json",
                    token_file=data_dir / "token.json",
                )
            self.assertIn("Codespaces", str(raised.exception))

    def test_cli_uses_shared_core_and_preserves_csv_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_analysis_fixture(data_dir)
            output_path = data_dir / "all.csv"

            with patch.object(
                extract_silent,
                "analyze",
                wraps=analytics_core.analyze,
            ) as shared_analyze, patch.object(
                sys,
                "argv",
                [
                    "extract_silent.py",
                    "--data-dir",
                    str(data_dir),
                    "--out",
                    str(output_path),
                    "--now",
                    "2026-08-20T12:00:00Z",
                ],
            ), redirect_stdout(StringIO()):
                extract_silent.main()

            result = pd.read_csv(output_path, dtype=str).fillna("")
            self.assertEqual(list(result.columns), extract_silent.OUTPUT_COLUMNS)
            self.assertEqual(
                list(result["channel_id"]),
                ["UC_DORMANT", "UC_NEW", "UC_OLD", "UC_ACTIVE"],
            )
            self.assertEqual(
                dict(zip(result["channel_id"], result["segment"])),
                {
                    "UC_DORMANT": extract_silent.SEG_DORMANT,
                    "UC_NEW": extract_silent.SEG_NEW_SILENT,
                    "UC_OLD": extract_silent.SEG_OLD_SILENT,
                    "UC_ACTIVE": extract_silent.SEG_ACTIVE,
                },
            )
            self.assertEqual(
                result.loc[result["channel_id"] == "UC_NEW", "subscribed_at_source"].item(),
                "api_published_at",
            )
            shared_analyze.assert_called_once()

    def test_cli_silent_filters_use_shared_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_analysis_fixture(data_dir)
            cases = [
                ("--never-commented", {"UC_NEW", "UC_OLD"}),
                (
                    "--no-comment-within",
                    {"UC_NEW", "UC_OLD", "UC_DORMANT"},
                ),
            ]
            for index, (flag, expected_channels) in enumerate(cases):
                with self.subTest(flag=flag):
                    output_path = data_dir / f"filtered-{index}.csv"
                    argv = [
                        "extract_silent.py",
                        "--data-dir",
                        str(data_dir),
                        "--out",
                        str(output_path),
                        "--now",
                        "2026-08-20T12:00:00Z",
                        flag,
                    ]
                    if flag == "--no-comment-within":
                        argv.append("90d")
                    with patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                        extract_silent.main()

                    result = pd.read_csv(output_path, dtype=str).fillna("")
                    self.assertEqual(set(result["channel_id"]), expected_channels)

    def test_cli_rejects_incomplete_comments_before_shared_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pd.DataFrame(
                [
                    [
                        "UC1",
                        "one",
                        "",
                        "2026-08-01T00:00:00Z",
                        "2026-08-20T00:00:00Z",
                        "1",
                    ]
                ],
                columns=collect_subscribers.REGISTRY_COLUMNS,
            ).to_csv(common.registry_path(data_dir), index=False)

            with patch.object(
                extract_silent,
                "analyze",
                create=True,
            ) as shared_analyze, patch.object(
                sys,
                "argv",
                ["extract_silent.py", "--data-dir", str(data_dir)],
            ):
                with self.assertRaises(SystemExit):
                    extract_silent.main()
            shared_analyze.assert_not_called()

    def test_notebook_uses_shared_core_without_legacy_calculation_calls(self):
        notebook_path = MODULE_DIR / "subscriber_analytics.ipynb"
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        code_cells = [
            "".join(cell.get("source", []))
            for cell in notebook["cells"]
            if cell.get("cell_type") == "code"
        ]
        notebook_code = "\n".join(code_cells)

        self.assertIn("analytics_core.analyze(", notebook_code)
        for legacy_call in (
            "extract_silent.build_table(",
            "extract_silent.add_segments(",
            "extract_silent.apply_filters(",
            "extract_silent.format_output(",
        ):
            with self.subTest(legacy_call=legacy_call):
                self.assertNotIn(legacy_call, notebook_code)
        for index, source in enumerate(code_cells):
            compile(source, f"subscriber_analytics.ipynb:cell-{index}", "exec")


if __name__ == "__main__":
    unittest.main()
