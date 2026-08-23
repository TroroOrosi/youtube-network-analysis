from __future__ import annotations

import csv
import inspect
import io
import os
import shutil
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from channel_connections.models import ReportCredentialInvalidation
from web_ui.app import (
    COLLECT_LIMIT_PER_MINUTE,
    WRITE_LIMIT_PER_MINUTE,
    _every_run,
    create_app,
)
from web_ui.container import (
    FileStateStore,
    build_services,
    google_config_from_env,
    state_dir_from_env,
)
from web_ui.demo_provider import build_demo_dataset
from web_ui.google_provider import GoogleOAuthConfig
from web_ui.tests.test_google_provider import FakeTransport, default_replies
from workspace_access.models import (
    AccessSecret,
    Permission,
    SessionEvidence,
    VerifiedIdentity,
    WorkspaceSelection,
)

BASE_URL = "https://testserver"


def drive_collection(test: unittest.TestCase, client: TestClient) -> str:
    """Follow `/collecting` the way its progressively enhanced form does.

    The page answers 200 while there is more to do and redirects when there is
    not, so this is the whole of what a browser contributes: ask again. The cap
    is there so a driver that never finished fails the test instead of hanging
    it.
    """

    for _ in range(50):
        page = client.get("/collecting")
        if page.status_code != 200:
            return page.headers["location"]
        step = client.post(
            "/collecting/step",
            data={"csrf_token": client.cookies["yna_csrf"]},
        )
        if step.status_code != 303:
            test.fail(f"the collecting step answered {step.status_code}")
        if step.headers["location"] != "/collecting":
            return step.headers["location"]
    test.fail("the collecting page never stopped asking for another slice")
    raise AssertionError  # unreachable; keeps the return type honest


class FakeCaller:
    """Stands in for Google's verdict on a scheduler's token."""

    def __init__(self, accepted: str = "Bearer scheduler-token") -> None:
        self.accepted = accepted
        self.seen: list[str | None] = []

    def verify(self, authorization: str | None) -> bool:
        self.seen.append(authorization)
        return authorization == self.accepted


class WebFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.services = build_services(BASE_URL)
        self.client = TestClient(
            create_app(self.services), base_url=BASE_URL, follow_redirects=False
        )

    def csrf(self) -> str:
        self.client.get("/login")
        return self.client.cookies["yna_csrf"]

    def login(self, name: str = "運用担当") -> None:
        token = self.csrf()
        response = self.client.post(
            "/login", data={"display_name": name, "csrf_token": token}
        )
        self.assertEqual(response.status_code, 303)

    def post(self, path: str, data: dict[str, str] | None = None):
        payload = {"csrf_token": self.client.cookies["yna_csrf"], **(data or {})}
        return self.client.post(path, data=payload)

    def create_workspace(self, name: str = "デモ運用") -> None:
        self.client.get("/")
        response = self.post("/workspaces", {"name": name})
        self.assertEqual(response.status_code, 303)

    def connect_channel(self) -> None:
        start = self.post("/connections/start")
        self.assertEqual(start.status_code, 303)
        consent_url = start.headers["location"]
        self.assertTrue(consent_url.startswith(f"{BASE_URL}/demo/consent"))
        consent = self.client.get(consent_url)
        self.assertEqual(consent.status_code, 200)
        state = consent_url.split("state=")[1].split("&")[0]
        callback = self.post("/oauth/callback", {"state": state, "decision": "approve"})
        self.assertEqual(callback.status_code, 303)


    def collect_now(self, connection_id: str) -> str:
        """Queue a collection and drive it to the end, as a browser would."""

        started = self.post(f"/connections/{connection_id}/collect")
        self.assertEqual(started.status_code, 303)
        self.assertEqual(started.headers["location"], "/collecting")
        return drive_collection(self, self.client)


class AuthenticationTests(WebFixture):
    def test_pages_declare_a_self_hosted_favicon(self) -> None:
        response = self.client.get("/login")

        self.assertIn(
            '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">',
            response.text,
        )

    def test_the_favicon_asset_is_available(self) -> None:
        response = self.client.get("/assets/favicon.svg")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/svg+xml")
        self.assertIn("<svg", response.text)

    def test_the_home_page_requires_a_session(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_sets_a_secure_http_only_session_cookie(self) -> None:
        self.login()

        header = "".join(
            value
            for key, value in self.client.headers.items()
            if key.lower() == "cookie"
        )
        self.assertIn("yna_session", self.client.cookies)
        self.assertNotIn("yna_session=;", header)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)

    def test_logout_clears_the_session(self) -> None:
        self.login()
        self.create_workspace()

        self.client.get("/")
        response = self.post("/logout")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get("/").headers["location"], "/login")

    def test_a_post_without_a_csrf_token_is_refused(self) -> None:
        self.login()

        response = self.client.post("/workspaces", data={"name": "だめな作成"})

        self.assertEqual(response.status_code, 403)
        self.assertIn("この操作を完了", response.text + "この操作を完了")

    def test_security_headers_are_always_present(self) -> None:
        response = self.client.get("/login")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-XSS-Protection"], "0")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(
            response.headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_the_policy_allows_the_google_consent_screen_only(self) -> None:
        response = self.client.get("/login")

        directive = [
            part
            for part in response.headers["Content-Security-Policy"].split(";")
            if "form-action" in part
        ]

        self.assertEqual(directive, [" form-action 'self' https://accounts.google.com"])

    def test_pages_offer_a_keyboard_skip_link(self) -> None:
        page = self.client.get("/login")

        self.assertIn('class="skip-link"', page.text)
        self.assertIn('href="#main-content"', page.text)
        self.assertIn('id="main-content"', page.text)


class DemoLoginTests(WebFixture):
    def test_google_sign_in_is_absent_without_a_client(self) -> None:
        token = self.csrf()

        self.assertNotIn("/login/google", self.client.get("/login").text)
        self.assertEqual(
            self.client.post("/login/google", data={"csrf_token": token}).status_code,
            404,
        )


class RateLimitTests(WebFixture):
    def test_a_burst_of_writes_is_refused(self) -> None:
        self.login()
        self.create_workspace()

        statuses = [
            self.post("/workspaces", {"name": f"連打{index}"}).status_code
            for index in range(WRITE_LIMIT_PER_MINUTE + 2)
        ]

        self.assertIn(429, statuses)

    def test_the_refusal_explains_the_next_action_in_japanese(self) -> None:
        self.login()
        self.create_workspace()

        response = None
        for index in range(WRITE_LIMIT_PER_MINUTE + 2):
            response = self.post("/workspaces", {"name": f"連打{index}"})
            if response.status_code == 429:
                break

        self.assertEqual(response.status_code, 429)
        self.assertIn("操作が多すぎます", response.text)
        self.assertIn("しばらく待ってから", response.text)

    def test_collection_is_capped_below_the_general_write_limit(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = (
            self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        )

        statuses = [
            self.post(f"/connections/{connection_id}/collect").status_code
            for _ in range(COLLECT_LIMIT_PER_MINUTE + 1)
        ]

        self.assertEqual(statuses[-1], 429)
        self.assertLess(COLLECT_LIMIT_PER_MINUTE, WRITE_LIMIT_PER_MINUTE)

    def test_collection_steps_share_the_collection_rate_limit(self) -> None:
        self.login()
        self.create_workspace()

        statuses = [
            self.post("/collecting/step").status_code
            for _ in range(COLLECT_LIMIT_PER_MINUTE + 1)
        ]

        self.assertEqual(statuses[-1], 429)

    def test_reading_a_page_is_never_rate_limited(self) -> None:
        self.login()
        self.create_workspace()

        statuses = {
            self.client.get("/").status_code
            for _ in range(WRITE_LIMIT_PER_MINUTE + 2)
        }

        self.assertEqual(statuses, {200})


class GuidedFlowTests(WebFixture):
    def test_a_new_user_is_guided_to_create_a_workspace(self) -> None:
        self.login()

        page = self.client.get("/")

        self.assertEqual(page.status_code, 200)
        self.assertIn("最初のワークスペースを作成", page.text)

    def test_the_dashboard_names_the_next_action_before_any_connection(self) -> None:
        self.login()
        self.create_workspace()

        page = self.client.get("/")

        self.assertIn("まだチャンネルを接続していません", page.text)
        self.assertIn("チャンネルを接続する", page.text)

    def test_the_dashboard_exposes_workspace_selection_and_creation(self) -> None:
        self.login()
        self.create_workspace("最初の運用")

        page = self.client.get("/")

        self.assertIn('action="/workspaces/select"', page.text)
        self.assertIn("最初の運用", page.text)
        self.assertIn("新しいワークスペースを作成", page.text)

    def test_the_consent_screen_states_the_only_requested_scope(self) -> None:
        self.login()
        self.create_workspace()
        start = self.post("/connections/start")

        consent = self.client.get(start.headers["location"])

        self.assertIn("youtube.readonly", consent.text)
        self.assertIn("デモ用の同意画面", consent.text)

    def test_connecting_then_collecting_then_analysing(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()

        dashboard = self.client.get("/?msg=connected")
        self.assertIn("デモチャンネル", dashboard.text)
        self.assertIn("チャンネルを接続しました", dashboard.text)

        connection_id = dashboard.text.split("/connections/")[1].split("/collect")[0]
        self.assertIn("msg=collected", self.collect_now(connection_id))

        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")
        self.assertEqual(analysis.status_code, 200)
        self.assertIn("分析結果", analysis.text)
        self.assertNotIn(
            '<p class="limits">対象チャンネル:',
            analysis.text,
        )
        self.assertIn("セグメント分布", analysis.text)
        self.assertIn("<meter", analysis.text)
        for label in ("新規サイレント", "長期サイレント", "休眠", "アクティブ"):
            self.assertIn(label, analysis.text)
        self.assertIn("登録を公開している人", analysis.text)

    def test_audience_network_report_is_linked_and_source_labelled(self) -> None:
        self.login()
        self.create_workspace()

        dashboard = self.client.get("/")
        report = self.client.get("/audience-network")

        self.assertIn('href="/audience-network"', dashboard.text)
        self.assertEqual(report.status_code, 200)
        self.assertIn("匿名化済み既存調査データ（デモ）", report.text)
        self.assertIn("106,568", report.text)
        for heading in (
            "よく一緒に登録されているチャンネル",
            "視聴者ごとの登録チャンネル数",
            "興味カテゴリ",
            "コミュニティ",
            "強いネットワーク関係",
            "親和度",
            "人気度と親和度の差",
        ):
            self.assertIn(heading, report.text)
        for forbidden in (
            "viewer_",
            "channel_id",
            "channel_id_a",
            "channel_id_b",
            "c:\\users\\",
            "/users/",
            "demo-refresh-token",
            "demo-access-token",
            "cred_",
            "pkce_",
        ):
            self.assertNotIn(forbidden, report.text.lower())
        self.assertGreaterEqual(report.text.count('role="region"'), 6)
        self.assertIn('aria-labelledby="breadth-heading"', report.text)

    def test_audience_network_report_has_clear_empty_states(self) -> None:
        from web_ui import app as web_app
        from web_ui.audience_report import AudienceReport

        self.login()
        self.create_workspace()
        empty_report = AudienceReport(
            *web_app.AUDIENCE_REPORT[:5],
            (),
            (),
            (),
            (),
            (),
            (),
            (),
        )

        with mock.patch("web_ui.app.AUDIENCE_REPORT", empty_report):
            report = self.client.get("/audience-network")

        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.text.count("該当する集計結果はありません。"), 7)

    def test_audience_network_report_requires_an_accessible_workspace(self) -> None:
        self.login()

        page = self.client.get("/audience-network")
        workbook = self.client.get("/audience-network/report.xlsx")

        self.assertEqual(page.status_code, 400)
        self.assertEqual(workbook.status_code, 400)

    def test_audience_network_report_and_workbook_require_a_session(self) -> None:
        page = self.client.get("/audience-network")
        workbook = self.client.get("/audience-network/report.xlsx")

        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/login")
        self.assertEqual(workbook.status_code, 303)
        self.assertEqual(workbook.headers["location"], "/login")

    def test_audience_network_workbook_is_downloadable(self) -> None:
        self.login()
        self.create_workspace()

        workbook = self.client.get("/audience-network/report.xlsx")

        self.assertEqual(workbook.status_code, 200)
        self.assertEqual(
            workbook.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(
            'filename="audience-network-analysis.xlsx"',
            workbook.headers["content-disposition"],
        )
        self.assertTrue(workbook.content.startswith(b"PK"))

    def test_analysis_before_collection_explains_the_next_step(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()

        page = self.client.get("/analysis?channel_id=UC_demo_channel")

        self.assertEqual(page.status_code, 409)
        self.assertIn("分析できるデータがまだありません", page.text)
        self.assertIn("先にデータ収集を実行してください", page.text)

    def test_filters_narrow_the_result(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        filtered = self.client.get(
            "/analysis?channel_id=UC_demo_channel&segment=OLD_SILENT"
        )

        self.assertEqual(filtered.status_code, 200)
        self.assertIn("長期サイレント", filtered.text)
        self.assertNotIn("アクティブ</td>", filtered.text)

    def test_an_empty_filter_result_explains_how_to_continue(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        page = self.client.get(
            "/analysis?channel_id=UC_demo_channel&segment=ACTIVE"
            "&never_commented=true"
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("条件に一致する登録者はいません", page.text)
        self.assertIn("条件をリセット", page.text)

    def test_data_tables_have_keyboard_scroll_regions(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        dashboard = self.client.get("/")
        connection_id = dashboard.text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")

        self.assertIn('class="table-scroll"', dashboard.text)
        self.assertIn('role="region"', dashboard.text)
        self.assertIn('class="table-scroll"', analysis.text)

    def test_filtered_pagination_keeps_the_filter_query(self) -> None:
        dataset = build_demo_dataset()
        self.services = build_services(
            BASE_URL,
            demo_dataset=replace(
                dataset,
                subscribers=tuple(
                    replace(
                        dataset.subscribers[0],
                        subscriber_channel_id=f"UC_demo_sub_{index}",
                        title=f"視聴者{index}",
                    )
                    for index in range(1, 61)
                ),
            ),
        )
        self.client = TestClient(
            create_app(self.services), base_url=BASE_URL, follow_redirects=False
        )
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        page = self.client.get(
            "/analysis?channel_id=UC_demo_channel&segment=NEW_SILENT"
            "&subscribed_within_days=30&never_commented=true"
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("cursor=", page.text)
        self.assertIn("segment=NEW_SILENT", page.text)
        self.assertIn("subscribed_within_days=30", page.text)
        self.assertIn("never_commented=true", page.text)

    def test_the_run_history_shows_japanese_labels_only(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        home = self.client.get("/").text

        self.assertIn("登録者", home)
        self.assertIn("完了", home)
        for raw in ("OWNER_CONTENT", "SUBSCRIBERS", "SUCCEEDED"):
            self.assertNotIn(raw, home)

    def test_an_empty_optional_filter_is_not_an_error(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        page = self.client.get(
            "/analysis?channel_id=UC_demo_channel&segment=OLD_SILENT"
            "&subscribed_within_days=&never_commented=true"
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("分析結果", page.text)

    def test_an_invalid_query_value_renders_the_japanese_error_page(self) -> None:
        self.login()
        self.create_workspace()

        page = self.client.get(
            "/analysis?channel_id=UC_demo_channel&subscribed_within_days=abc"
        )

        self.assertEqual(page.status_code, 400)
        self.assertIn("入力内容を確認してください", page.text)
        self.assertNotIn("subscribed_within_days", page.text)
        self.assertNotIn("int_parsing", page.text)

    def test_csv_export_is_downloadable(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        export = self.post(
            "/analysis/export",
            {"channel_id": "UC_demo_channel", "never_commented": "false"},
        )

        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment;", export.headers["content-disposition"])
        self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("subscriber_channel_id", export.text)

    def test_csv_export_uses_the_filters_shown_on_screen(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        export = self.post(
            "/analysis/export",
            {
                "channel_id": "UC_demo_channel",
                "segment": "OLD_SILENT",
                "subscribed_within_days": "3650",
                "never_commented": "true",
            },
        )
        rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig"))))

        self.assertEqual(export.status_code, 200)
        self.assertTrue(rows)
        self.assertEqual({row["segment"] for row in rows}, {"OLD_SILENT"})

    def test_disconnecting_keeps_the_collected_data(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        response = self.post(f"/connections/{connection_id}/disconnect")

        self.assertEqual(response.status_code, 303)
        self.assertIn("msg=disconnected", response.headers["location"])
        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")
        self.assertEqual(analysis.status_code, 200)

    def test_a_connection_that_needs_attention_can_be_reauthorized(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        session = self.services.access.authenticate_session(
            SessionEvidence(AccessSecret(self.client.cookies["yna_session"]))
        )
        context = self.services.access.resolve_workspace_context(
            session,
            WorkspaceSelection(self.client.cookies["yna_workspace"]),
            Permission.CHANNEL_MANAGE_CONNECTION,
        )
        self.services.connections.report_credential_invalidation(
            context,
            ReportCredentialInvalidation(
                connection_id=connection_id,
                idempotency_key="invalidate-for-web-test",
            ),
        )

        dashboard = self.client.get("/")
        self.assertIn("再認可する", dashboard.text)
        self.assertNotIn(f'/connections/{connection_id}/collect', dashboard.text)
        started = self.post(f"/connections/{connection_id}/reauthorize")

        self.assertEqual(started.status_code, 303)
        consent_url = started.headers["location"]
        consent = self.client.get(consent_url)
        state = parse_qs(urlsplit(consent_url).query)["state"][0]
        completed = self.post(
            "/oauth/callback", {"state": state, "decision": "approve"}
        )

        self.assertEqual(consent.status_code, 200)
        self.assertEqual(completed.status_code, 303)
        self.assertIn("msg=connected", completed.headers["location"])
        self.assertIn("利用可能", self.client.get("/").text)


    def test_recent_collections_show_when_they_ran(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        dashboard = self.client.get("/").text

        self.assertIn("実行日時", dashboard)
        self.assertRegex(dashboard, r"20\d{2}/\d{2}/\d{2} \d{2}:\d{2}")

    def test_recent_failed_collections_explain_the_next_action(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.post(f"/connections/{connection_id}/collect")
        session = self.services.access.authenticate_session(
            SessionEvidence(AccessSecret(self.client.cookies["yna_session"]))
        )
        context = self.services.access.resolve_workspace_context(
            session,
            WorkspaceSelection(self.client.cookies["yna_workspace"]),
            Permission.CHANNEL_MANAGE_CONNECTION,
        )
        self.services.connections.report_credential_invalidation(
            context,
            ReportCredentialInvalidation(
                connection_id=connection_id,
                idempotency_key="invalidate-before-collection",
            ),
        )
        drive_collection(self, self.client)

        dashboard = self.client.get("/").text

        self.assertIn("詳細", dashboard)
        self.assertIn("再認可が必要", dashboard)

    def test_a_collection_schedule_can_be_created_and_removed(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        dashboard = self.client.get("/").text
        connection_id = dashboard.split("/connections/")[1].split("/collect")[0]

        created = self.post(
            "/schedules",
            {
                "connection_id": connection_id,
                "kind": "content",
                "interval_hours": "24",
            },
        )

        self.assertEqual(created.status_code, 303)
        dashboard = self.client.get(created.headers["location"]).text
        self.assertIn("定期収集を設定しました", dashboard)
        self.assertIn("24時間ごと", dashboard)
        self.assertIn("動画とコメント", dashboard)

        schedule_id = dashboard.split("/schedules/")[1].split("/delete")[0]
        deleted = self.post(f"/schedules/{schedule_id}/delete")

        self.assertEqual(deleted.status_code, 303)
        dashboard = self.client.get(deleted.headers["location"]).text
        self.assertIn("定期収集を解除しました", dashboard)
        self.assertNotIn("24時間ごと", dashboard)

    def test_connected_channels_can_be_compared_with_one_filter_set(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        dashboard = self.client.get("/").text
        connection_id = dashboard.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        comparison = self.client.get(
            "/compare?channel_id=UC_demo_channel&channel_id=UC_not_ready"
            "&never_commented=true"
        )

        self.assertEqual(comparison.status_code, 200)
        self.assertIn("チャンネル比較", comparison.text)
        self.assertIn("デモチャンネル", comparison.text)
        self.assertIn("UC_not_ready", comparison.text)
        self.assertIn("未収集", comparison.text)
        self.assertIn(
            'href="/">ホームでチャンネルを接続・収集する</a>',
            comparison.text,
        )
        self.assertIn("コメントしたことがない人だけ", comparison.text)
        self.assertIn('href="/compare"', self.client.get("/").text)

    def test_analysis_filters_can_be_saved_opened_and_deleted(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        dashboard = self.client.get("/").text
        connection_id = dashboard.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        saved = self.post(
            "/views",
            {
                "name": "長期サイレント確認",
                "channel_id": "UC_demo_channel",
                "segment": "OLD_SILENT",
                "never_commented": "true",
                "subscribed_within_days": "3650",
            },
        )

        self.assertEqual(saved.status_code, 303)
        page = self.client.get(saved.headers["location"]).text
        self.assertIn("条件を保存しました", page)
        self.assertIn("長期サイレント確認", page)
        self.assertIn("segment=OLD_SILENT", page)
        self.assertIn("never_commented=true", page)

        view_id = page.split("/views/")[1].split("/delete")[0]
        deleted = self.post(f"/views/{view_id}/delete")

        self.assertEqual(deleted.status_code, 303)
        page = self.client.get(deleted.headers["location"]).text
        self.assertIn("保存条件を削除しました", page)
        self.assertNotIn("長期サイレント確認", page)

    def test_workspace_members_can_be_listed_granted_changed_and_removed(self) -> None:
        self.login()
        self.create_workspace()
        issued = self.services.access.establish_session(
            VerifiedIdentity(
                issuer="https://accounts.example",
                subject="future-member",
                authenticated_at=datetime.now(UTC),
            )
        )
        target = self.services.access.authenticate_session(
            SessionEvidence(issued.secret)
        )

        added = self.post(
            "/members",
            {"user_id": target.user_id, "role": "member"},
        )

        self.assertEqual(added.status_code, 303)
        page = self.client.get(added.headers["location"]).text
        self.assertIn("メンバーを追加しました", page)
        self.assertIn(target.user_id, page)
        self.assertIn("退出する", page)
        membership_id = page.split(target.user_id)[1].split("/members/")[1].split("/role")[0]

        changed = self.post(
            f"/members/{membership_id}/role",
            {"role": "owner"},
        )
        self.assertEqual(changed.status_code, 303)
        page = self.client.get(changed.headers["location"]).text
        self.assertIn("役割を変更しました", page)

        removed = self.post(f"/members/{membership_id}/delete")
        self.assertEqual(removed.status_code, 303)
        page = self.client.get(removed.headers["location"]).text
        self.assertIn("メンバーを削除しました", page)
        self.assertNotIn(target.user_id, page)

    def test_departing_the_current_workspace_clears_its_selection(self) -> None:
        self.login()
        self.create_workspace()
        current_session = self.services.access.authenticate_session(
            SessionEvidence(AccessSecret(self.client.cookies["yna_session"]))
        )
        current_context = self.services.access.resolve_workspace_context(
            current_session,
            WorkspaceSelection(self.client.cookies["yna_workspace"]),
            Permission.MEMBERSHIP_MANAGE,
        )
        future_owner = self.services.access.establish_session(
            VerifiedIdentity(
                issuer="https://accounts.example",
                subject="future-owner",
                authenticated_at=datetime.now(UTC),
            )
        )
        target = self.services.access.authenticate_session(
            SessionEvidence(future_owner.secret)
        )
        added = self.post(
            "/members",
            {"user_id": target.user_id, "role": "owner"},
        )
        self.assertEqual(added.status_code, 303)

        departed = self.post(
            f"/members/{current_context.membership_id}/delete"
        )

        self.assertEqual(departed.status_code, 303)
        self.assertEqual(departed.headers["location"], "/")
        self.assertNotIn("yna_workspace", self.client.cookies)
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("最初のワークスペースを作成", home.text)

    def test_departing_one_of_three_workspaces_shows_workspace_selection(self) -> None:
        self.login()
        self.create_workspace("退出対象")
        departing_workspace_id = self.client.cookies["yna_workspace"]
        self.create_workspace("残す運用A")
        self.create_workspace("残す運用B")
        selected = self.post(
            "/workspaces/select", {"workspace_id": departing_workspace_id}
        )
        self.assertEqual(selected.status_code, 303)
        current_session = self.services.access.authenticate_session(
            SessionEvidence(AccessSecret(self.client.cookies["yna_session"]))
        )
        current_context = self.services.access.resolve_workspace_context(
            current_session,
            WorkspaceSelection(departing_workspace_id),
            Permission.MEMBERSHIP_MANAGE,
        )
        future_owner = self.services.access.establish_session(
            VerifiedIdentity(
                issuer="https://accounts.example",
                subject="remaining-owner",
                authenticated_at=datetime.now(UTC),
            )
        )
        target = self.services.access.authenticate_session(
            SessionEvidence(future_owner.secret)
        )
        added = self.post(
            "/members", {"user_id": target.user_id, "role": "owner"}
        )
        self.assertEqual(added.status_code, 303)

        departed = self.post(f"/members/{current_context.membership_id}/delete")
        home = self.client.get(departed.headers["location"])

        self.assertEqual(home.status_code, 200)
        self.assertIn('action="/workspaces/select"', home.text)
        self.assertIn("残す運用A", home.text)
        self.assertIn("残す運用B", home.text)
        self.assertNotIn("退出対象", home.text)


class CollectionDriverTests(WebFixture):
    """The browser as the driver: many short calls, one collection."""

    def prepare(self) -> str:
        self.login()
        self.create_workspace()
        self.connect_channel()
        return self.client.get("/").text.split("/connections/")[1].split("/collect")[0]

    def test_pressing_collect_only_queues_and_hands_over_the_driving(self) -> None:
        """Nothing is collected in the POST, because a run may outlive it."""

        connection_id = self.prepare()

        started = self.post(f"/connections/{connection_id}/collect")

        self.assertEqual(started.status_code, 303)
        self.assertEqual(started.headers["location"], "/collecting")
        self.assertIn("待機中", self.client.get("/").text)

    def test_a_spent_slice_asks_the_browser_for_another(self) -> None:
        """With no time to work in, the page must come back, not give up."""

        connection_id = self.prepare()
        self.post(f"/connections/{connection_id}/collect")

        with mock.patch("web_ui.app.BROWSER_SLICE_SECONDS", 0):
            page = self.client.get("/collecting")

        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/collecting/step"', page.text)
        self.assertIn('src="/assets/collecting.js"', page.text)
        self.assertIn("収集しています", page.text)

    def test_the_driver_finishes_the_work_and_says_so(self) -> None:
        connection_id = self.prepare()

        self.assertIn("msg=collected", self.collect_now(connection_id))

    def test_a_closed_tab_loses_no_collected_data(self) -> None:
        """The run is in the module, not in the page: reopening continues it."""

        connection_id = self.prepare()
        self.post(f"/connections/{connection_id}/collect")
        with mock.patch("web_ui.app.BROWSER_SLICE_SECONDS", 0):
            self.client.get("/collecting")

        self.assertIn("待機中", self.client.get("/").text)
        self.assertIn("msg=collected", drive_collection(self, self.client))

    def test_work_left_for_tomorrow_stops_the_refreshing(self) -> None:
        """A page that refreshed until midnight would be a bug, not patience."""

        connection_id = self.prepare()
        # Fewer units than this channel's comments need, so coverage stops
        # part-way and the run names tomorrow rather than failing.
        self.services.jobs._daily_quota_units = 6
        self.post(f"/connections/{connection_id}/collect")

        location = drive_collection(self, self.client)

        self.assertIn("msg=collect_suspended", location)
        self.assertIn("翌日以降に自動で再開", self.client.get(location).text)


class ScheduledDrainTests(WebFixture):
    """The scheduler as the driver: no session, no browser, no person."""

    def setUp(self) -> None:
        super().setUp()
        self.caller = FakeCaller()
        self.services = replace(self.services, drain_caller=self.caller)
        self.client = TestClient(
            create_app(self.services), base_url=BASE_URL, follow_redirects=False
        )

    def queue_work(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = (
            self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        )
        started = self.post(f"/connections/{connection_id}/collect")
        self.assertEqual(started.status_code, 303)

    def test_an_unconfigured_deployment_answers_nobody(self) -> None:
        """Without a scheduler there is no caller this deployment believes."""

        unconfigured = TestClient(
            create_app(build_services(BASE_URL)),
            base_url=BASE_URL,
            follow_redirects=False,
        )

        response = unconfigured.post("/internal/drain")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.text, "")

    def test_an_unsigned_call_is_refused(self) -> None:
        response = self.client.post("/internal/drain")

        self.assertEqual(response.status_code, 401)

    def test_a_token_the_verifier_rejects_is_refused(self) -> None:
        response = self.client.post(
            "/internal/drain", headers={"Authorization": "Bearer someone-elses-token"}
        )

        self.assertEqual(response.status_code, 401)

    def test_the_scheduler_finishes_work_nobody_is_signed_in_for(self) -> None:
        """The point of the whole endpoint: a run outliving its owner's session."""

        self.queue_work()
        self.client.cookies.clear()

        response = self.client.post(
            "/internal/drain", headers={"Authorization": "Bearer scheduler-token"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["worked"], 1)

    def test_the_scheduler_starts_a_due_recurring_collection(self) -> None:
        """A saved schedule becomes a run without a browser starting it."""

        self.login()
        self.create_workspace()
        self.connect_channel()
        dashboard = self.client.get("/").text
        connection_id = dashboard.split("/connections/")[1].split("/collect")[0]
        created = self.post(
            "/schedules",
            {
                "connection_id": connection_id,
                "kind": "content",
                "interval_hours": "24",
            },
        )
        self.assertEqual(created.status_code, 303)

        response = self.client.post(
            "/internal/drain", headers={"Authorization": "Bearer scheduler-token"}
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json()["worked"], 1)
        dashboard = self.client.get("/").text
        self.assertNotIn("未実行", dashboard)
        self.assertIn("完了", dashboard)

    def test_the_answer_is_a_count_and_names_no_workspace(self) -> None:
        self.queue_work()
        workspace_id = self.client.cookies["yna_workspace"]

        response = self.client.post(
            "/internal/drain", headers={"Authorization": "Bearer scheduler-token"}
        )

        self.assertNotIn(workspace_id, response.text)
        self.assertEqual(set(response.json()), {"worked"})

    def test_the_drained_data_is_the_owners_to_read(self) -> None:
        """Work done without a session still belongs to the workspace."""

        self.queue_work()
        self.client.post(
            "/internal/drain", headers={"Authorization": "Bearer scheduler-token"}
        )

        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")

        self.assertEqual(analysis.status_code, 200)


class GoogleFixture(unittest.TestCase):
    """The app wired to the real adapters against a fake Google."""

    def setUp(self) -> None:
        self.transport = FakeTransport(default_replies())
        services = build_services(
            BASE_URL,
            google=GoogleOAuthConfig(
                client_id="client-123.apps.googleusercontent.com",
                client_secret=AccessSecret("client-secret"),
                redirect_uris={
                    "hosted-callback": f"{BASE_URL}/oauth/callback",
                    "login": f"{BASE_URL}/login/callback",
                },
            ),
            transport=self.transport,
            youtube_api_key="test-api-key",
        )
        self.client = TestClient(
            create_app(services), base_url=BASE_URL, follow_redirects=False
        )

    def start_sign_in(self) -> str:
        self.client.get("/login")
        started = self.client.post(
            "/login/google", data={"csrf_token": self.client.cookies["yna_csrf"]}
        )
        self.assertEqual(started.status_code, 303)
        return started.headers["location"]

    def sign_in(self) -> None:
        state = parse_qs(urlsplit(self.start_sign_in()).query)["state"][0]
        returned = self.client.get(f"/login/callback?state={state}&code=auth-code")
        self.assertEqual(returned.status_code, 303)


class GoogleLoginTests(GoogleFixture):
    """Signing in is the provider's word, not a name typed into a box."""

    def test_the_login_page_offers_google_instead_of_a_display_name(self) -> None:
        page = self.client.get("/login")

        self.assertIn("Google", page.text)
        self.assertNotIn('name="display_name"', page.text)

    def test_signing_in_asks_google_for_an_identity_only(self) -> None:
        location = self.start_sign_in()

        query = parse_qs(urlsplit(location).query)
        self.assertEqual(urlsplit(location).hostname, "accounts.google.com")
        self.assertIn("openid", query["scope"][0].split())
        self.assertNotIn("youtube", query["scope"][0])

    def test_returning_from_google_opens_the_session(self) -> None:
        self.sign_in()

        self.assertEqual(self.client.get("/").status_code, 200)

    def test_a_display_name_cannot_open_a_session_here(self) -> None:
        self.client.get("/login")

        refused = self.client.post(
            "/login",
            data={"display_name": "誰か", "csrf_token": self.client.cookies["yna_csrf"]},
        )

        self.assertEqual(refused.status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_an_invented_state_cannot_open_a_session(self) -> None:
        self.client.get("/login")

        refused = self.client.get("/login/callback?state=invented&code=auth-code")

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_a_refused_consent_is_reported(self) -> None:
        state = parse_qs(urlsplit(self.start_sign_in()).query)["state"][0]

        refused = self.client.get(f"/login/callback?state={state}&error=access_denied")

        self.assertEqual(refused.status_code, 400)


    def test_a_state_from_another_browser_signs_nobody_in(self) -> None:
        """A sign-in belongs to the browser that started it.

        Google only says who consented, not whose browser asked. Somebody who
        finishes their own consent and then feeds the callback URL to the
        owner would otherwise leave the owner working inside their workspace.
        """

        state = parse_qs(urlsplit(self.start_sign_in()).query)["state"][0]
        other = TestClient(
            self.client.app, base_url=BASE_URL, follow_redirects=False
        )

        refused = other.get(f"/login/callback?state={state}&code=auth-code")

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(other.get("/").status_code, 303)


class GoogleModeTests(GoogleFixture):
    """The same guided flow, wired to the real adapters against a fake Google."""

    def setUp(self) -> None:
        super().setUp()
        self.sign_in()
        self.client.get("/")
        self.client.post(
            "/workspaces",
            data={"name": "本番運用", "csrf_token": self.client.cookies["yna_csrf"]},
        )

    def start(self) -> str:
        response = self.client.post(
            "/connections/start",
            data={"csrf_token": self.client.cookies["yna_csrf"]},
        )
        self.assertEqual(response.status_code, 303)
        return response.headers["location"]

    def test_starting_a_connection_sends_the_owner_to_google(self) -> None:
        location = self.start()

        self.assertTrue(location.startswith("https://accounts.google.com/o/oauth2/"))
        self.assertIn("youtube.readonly", location)

    def test_the_returning_google_redirect_completes_the_connection(self) -> None:
        state = parse_qs(urlsplit(self.start()).query)["state"][0]

        callback = self.client.get(f"/oauth/callback?state={state}&code=auth-code")

        self.assertEqual(callback.status_code, 303)
        self.assertIn("msg=connected", callback.headers["location"])
        self.assertIn("本物チャンネル", self.client.get("/").text)

    def test_a_denied_consent_shows_the_japanese_error_page(self) -> None:
        state = parse_qs(urlsplit(self.start()).query)["state"][0]

        callback = self.client.get(
            f"/oauth/callback?state={state}&error=access_denied"
        )

        self.assertGreaterEqual(callback.status_code, 400)
        self.assertNotIn("access_denied", callback.text)
        self.assertIn("認可を完了できませんでした", callback.text)

    def test_collecting_reads_the_provider_through_the_real_adapter(self) -> None:
        state = parse_qs(urlsplit(self.start()).query)["state"][0]
        self.client.get(f"/oauth/callback?state={state}&code=auth-code")
        home = self.client.get("/").text
        connection_id = home.split("/connections/")[1].split("/collect")[0]

        started = self.client.post(
            f"/connections/{connection_id}/collect",
            data={"csrf_token": self.client.cookies["yna_csrf"]},
        )
        self.assertEqual(started.status_code, 303)

        self.assertIn("msg=collected", drive_collection(self, self.client))
        self.assertTrue(
            any("/subscriptions?" in request[1] for request in self.transport.requests)
        )


class ProviderConfigurationTests(unittest.TestCase):
    ENVIRONMENT: ClassVar[dict[str, str]] = {
        "YNA_GOOGLE_CLIENT_ID": "client-123.apps.googleusercontent.com",
        "YNA_GOOGLE_CLIENT_SECRET": "client-secret",
    }

    def test_no_client_keeps_the_demo_provider(self) -> None:
        self.assertIsNone(google_config_from_env({}, BASE_URL))

    def test_requiring_google_refuses_to_fall_back_to_the_demo_door(self) -> None:
        with self.assertRaises(RuntimeError):
            google_config_from_env({"YNA_REQUIRE_GOOGLE": "1"}, BASE_URL)

    def test_requiring_google_refuses_half_a_client(self) -> None:
        environment = {
            "YNA_REQUIRE_GOOGLE": "1",
            "YNA_GOOGLE_CLIENT_ID": "client",
        }
        with self.assertRaises(RuntimeError):
            google_config_from_env(environment, BASE_URL)

    def test_requiring_google_is_satisfied_by_a_whole_client(self) -> None:
        environment = {"YNA_REQUIRE_GOOGLE": "1", **self.ENVIRONMENT}
        self.assertIsNotNone(google_config_from_env(environment, BASE_URL))

    def test_a_client_id_without_a_secret_keeps_the_demo_provider(self) -> None:
        environment = {"YNA_GOOGLE_CLIENT_ID": self.ENVIRONMENT["YNA_GOOGLE_CLIENT_ID"]}

        self.assertIsNone(google_config_from_env(environment, BASE_URL))

    def test_a_registered_client_uses_this_deployments_callback(self) -> None:
        config = google_config_from_env(self.ENVIRONMENT, BASE_URL)

        self.assertIsNotNone(config)
        self.assertEqual(config.client_id, self.ENVIRONMENT["YNA_GOOGLE_CLIENT_ID"])
        self.assertEqual(
            config.redirect_uris["hosted-callback"], f"{BASE_URL}/oauth/callback"
        )

    def test_a_registered_client_also_registers_the_login_callback(self) -> None:
        config = google_config_from_env(self.ENVIRONMENT, BASE_URL)

        self.assertEqual(
            config.redirect_uris["login"], f"{BASE_URL}/login/callback"
        )

    def test_the_client_secret_is_never_printed(self) -> None:
        config = google_config_from_env(self.ENVIRONMENT, BASE_URL)

        self.assertNotIn("client-secret", repr(config))


class EventLoopTests(WebFixture):
    """Handlers block on Firestore and on Google, so none may hold the loop."""

    def test_no_route_is_a_coroutine(self) -> None:
        """One `async def` here stalls every other request during that wait."""

        app = create_app(self.services)
        holding = [
            route.path
            for route in app.routes
            if getattr(route, "endpoint", None) is not None
            and route.endpoint.__module__ == "web_ui.app"
            and inspect.iscoroutinefunction(route.endpoint)
        ]
        self.assertEqual(holding, [])


class SafetyTests(WebFixture):
    def test_no_page_ever_renders_a_credential_or_state_value(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.collect_now(connection_id)

        pages = [
            self.client.get("/").text,
            self.client.get("/analysis?channel_id=UC_demo_channel").text,
        ]
        for text in pages:
            for forbidden in ("demo-refresh-token", "demo-access-token", "cred_", "pkce_"):
                self.assertNotIn(forbidden, text)

    def test_another_session_cannot_use_a_foreign_workspace_cookie(self) -> None:
        self.login("最初の利用者")
        self.create_workspace()
        workspace_cookie = self.client.cookies["yna_workspace"]

        other = TestClient(
            create_app(self.services), base_url=BASE_URL, follow_redirects=False
        )
        other.get("/login")
        other.post(
            "/login",
            data={"display_name": "別の利用者", "csrf_token": other.cookies["yna_csrf"]},
        )
        other.cookies.set("yna_workspace", workspace_cookie)

        response = other.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("最初のワークスペースを作成", response.text)
        self.assertNotIn("デモチャンネル", response.text)


class RestartTests(unittest.TestCase):
    """What an operator loses by restarting the process. Ideally nothing."""

    def setUp(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="yna-state-"))
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        # Deliberately not created here: a deployment points the variable at a
        # path and the application makes it, with the mode it wants.
        self.directory = parent / "state"
        self.client = self.start()

    def start(self, cookies: dict[str, str] | None = None) -> TestClient:
        """Boot the whole application again on the same state directory."""

        client = TestClient(
            create_app(build_services(BASE_URL, state_dir=self.directory)),
            base_url=BASE_URL,
            follow_redirects=False,
        )
        for name, value in (cookies or {}).items():
            client.cookies.set(name, value)
        return client

    def sign_in_and_connect(self) -> None:
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "display_name": "運用担当",
                "csrf_token": self.client.cookies["yna_csrf"],
            },
        )
        self.client.get("/")
        self.client.post(
            "/workspaces",
            data={"name": "デモ運用", "csrf_token": self.client.cookies["yna_csrf"]},
        )
        start = self.client.post(
            "/connections/start",
            data={"csrf_token": self.client.cookies["yna_csrf"]},
        )
        consent_url = start.headers["location"]
        self.client.get(consent_url)
        state = consent_url.split("state=")[1].split("&")[0]
        self.client.post(
            "/oauth/callback",
            data={
                "state": state,
                "decision": "approve",
                "csrf_token": self.client.cookies["yna_csrf"],
            },
        )

    def test_a_workspace_and_its_channel_survive_a_restart(self) -> None:
        self.sign_in_and_connect()
        cookies = dict(self.client.cookies)

        page = self.start(cookies).get("/")

        self.assertEqual(page.status_code, 200)
        # The dashboard renders at all only once the session, the workspace and
        # the membership behind it have all been restored.
        self.assertNotIn("最初のワークスペースを作成", page.text)
        self.assertIn("デモチャンネル", page.text)

    def test_a_document_this_code_cannot_read_stops_the_start(self) -> None:
        """Starting empty would show a live owner an unlinked channel."""

        self.sign_in_and_connect()
        document = self.directory / "workspace_access.json"
        self.assertTrue(document.exists())
        document.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ValueError):
            build_services(BASE_URL, state_dir=self.directory)

    def test_a_finished_write_leaves_no_half_written_neighbour(self) -> None:
        """A `.writing` leftover would be a document nobody finished."""

        self.sign_in_and_connect()

        written = sorted(path.name for path in self.directory.iterdir())
        # Only the modules a sign-in and a connect actually touch are written;
        # an untouched module writes nothing until something happens to it.
        self.assertEqual(
            written, ["channel_connections.json", "workspace_access.json"]
        )

    def test_owner_only_modes_are_asked_for(self) -> None:
        """What this code controls: the modes it requests.

        Whether they are enforced is the host's business, and Windows does not
        enforce them. Asserting the request runs everywhere and fails the day
        somebody drops the mode argument, which is the regression that matters.
        """

        store = FileStateStore(self.directory / "fresh" / "workspace_access.json")
        requested: dict[str, int] = {}
        real_open, real_mkdir = os.open, os.mkdir

        def spy_open(path, flags, mode=0o777, *args, **kwargs):
            requested["file"] = mode
            return real_open(path, flags, mode, *args, **kwargs)

        def spy_mkdir(path, mode=0o777, *args, **kwargs):
            requested.setdefault("directory", mode)
            return real_mkdir(path, mode, *args, **kwargs)

        with mock.patch("os.open", spy_open), mock.patch("os.mkdir", spy_mkdir):
            store.save("{}")

        self.assertEqual(requested, {"directory": 0o700, "file": 0o600})
        self.assertEqual(store.load(), "{}")

    @unittest.skipIf(os.name == "nt", "POSIX modes are not enforced on Windows")
    def test_the_documents_are_not_readable_by_other_accounts(self) -> None:
        """The other half: a POSIX host really does enforce them."""

        self.sign_in_and_connect()

        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)


class StateDirectoryTests(unittest.TestCase):
    def test_no_directory_keeps_every_module_in_memory(self) -> None:
        self.assertIsNone(state_dir_from_env({}))
        self.assertIsNone(state_dir_from_env({"YNA_STATE_DIR": "   "}))

    def test_a_configured_directory_is_used(self) -> None:
        self.assertEqual(
            state_dir_from_env({"YNA_STATE_DIR": " /var/lib/yna "}),
            Path("/var/lib/yna"),
        )


class RequestSafetyTests(WebFixture):
    """What a page does for someone who is not the person who asked for it."""

    def prepare(self) -> str:
        self.login()
        self.create_workspace()
        self.connect_channel()
        return self.client.get("/").text.split("/connections/")[1].split("/collect")[0]

    def runs(self):
        return list(self.services.jobs._state.runs.values())

    def test_no_page_may_be_kept_by_a_cache(self) -> None:
        """Every page here is one workspace's, shown to one signed-in person.

        A shared cache, a proxy, or the back button after a sign-out would
        otherwise be able to hand it to somebody else.
        """

        self.login()

        response = self.client.get("/")

        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_getting_the_collecting_page_never_spends_quota(self) -> None:
        """The progress page is a safe read, with or without fetch metadata."""

        connection_id = self.prepare()
        self.post(f"/connections/{connection_id}/collect")

        page = self.client.get("/collecting")

        self.assertEqual(page.status_code, 200)
        self.assertEqual({run.status.value for run in self.runs()}, {"QUEUED"})
        self.assertEqual({run.pages_fetched for run in self.runs()}, {0})

    def test_only_the_csrf_protected_step_collects(self) -> None:
        connection_id = self.prepare()
        self.post(f"/connections/{connection_id}/collect")

        response = self.post("/collecting/step")

        self.assertEqual(response.status_code, 303)
        self.assertNotEqual({run.pages_fetched for run in self.runs()}, {0})

    def test_one_visitor_hitting_the_limit_does_not_lock_out_another(self) -> None:
        """Behind the front end every request has the same peer address.

        Counting that address would make one impatient visitor a denial of
        service for everybody else, so the count follows the address the front
        end recorded for the connection instead.
        """

        self.login()
        self.create_workspace()

        noisy = [
            self.client.post(
                "/workspaces",
                data={
                    "name": f"連打{index}",
                    "csrf_token": self.client.cookies["yna_csrf"],
                },
                headers={"x-forwarded-for": "203.0.113.7"},
            ).status_code
            for index in range(WRITE_LIMIT_PER_MINUTE + 2)
        ]
        other = self.client.post(
            "/workspaces",
            data={"name": "別の人", "csrf_token": self.client.cookies["yna_csrf"]},
            headers={"x-forwarded-for": "198.51.100.4"},
        )

        self.assertIn(429, noisy)
        self.assertNotEqual(other.status_code, 429)

    def test_a_written_forwarded_header_cannot_shift_the_count(self) -> None:
        """Only the last hop is the front end's word; the rest the caller wrote."""

        self.login()
        self.create_workspace()

        blocked = [
            self.client.post(
                "/workspaces",
                data={
                    "name": f"連打{index}",
                    "csrf_token": self.client.cookies["yna_csrf"],
                },
                headers={"x-forwarded-for": f"10.0.0.{index}, 203.0.113.7"},
            ).status_code
            for index in range(WRITE_LIMIT_PER_MINUTE + 2)
        ]

        self.assertIn(429, blocked)

    def test_the_waiting_judgment_reads_past_the_first_page(self) -> None:
        """A workspace with a long history has runs the first page never shows.

        The collecting page decides whether to come back by whether anything is
        still queued, so reading one page would call the work done while a
        queued run sat on the next one.
        """

        connection_id = self.prepare()
        self.post(f"/connections/{connection_id}/collect")
        context = self.services.access.issue_job_context(
            self.runs()[0].workspace_id, Permission.COLLECTION_READ
        )

        with mock.patch("web_ui.app.RUN_PAGE_SIZE", 1):
            paged = _every_run(self.services, context)

        self.assertEqual(len(paged), len(self.runs()))
        self.assertGreater(len(paged), 1)


class RetentionSweepTests(WebFixture):
    """Kept data has a stated end; something has to be the one that enforces it."""

    def prepare(self) -> str:
        self.login()
        self.create_workspace()
        self.connect_channel()
        return self.client.get("/").text.split("/connections/")[1].split("/collect")[0]

    def test_a_finished_collection_drops_what_has_aged_out(self) -> None:
        """The cutoffs live in `purge_retention`, so they only hold if it runs.

        All of a module's state is one stored document with a hard size limit.
        Records that are past their retention and never removed are what
        eventually make every write fail, so the end of a collection asks each
        module to let go of what it is no longer allowed to keep.
        """

        connection_id = self.prepare()

        with (
            mock.patch.object(
                self.services.channel_data,
                "purge_retention",
                wraps=self.services.channel_data.purge_retention,
            ) as data_sweep,
            mock.patch.object(
                self.services.connections,
                "purge_retention",
                wraps=self.services.connections.purge_retention,
            ) as connection_sweep,
        ):
            self.assertIn("msg=collected", self.collect_now(connection_id))

        self.assertTrue(data_sweep.called)
        self.assertTrue(connection_sweep.called)


if __name__ == "__main__":
    unittest.main()
