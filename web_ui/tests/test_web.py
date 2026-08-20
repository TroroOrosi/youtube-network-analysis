from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from web_ui.app import create_app
from web_ui.container import build_services


BASE_URL = "https://testserver"


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


class AuthenticationTests(WebFixture):
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
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


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
        collected = self.post(f"/connections/{connection_id}/collect")
        self.assertEqual(collected.status_code, 303)
        self.assertIn("msg=collected", collected.headers["location"])

        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")
        self.assertEqual(analysis.status_code, 200)
        self.assertIn("分析結果", analysis.text)
        for label in ("新規サイレント", "長期サイレント", "休眠", "アクティブ"):
            self.assertIn(label, analysis.text)
        self.assertIn("登録を公開している人", analysis.text)

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
        self.post(f"/connections/{connection_id}/collect")

        filtered = self.client.get(
            "/analysis?channel_id=UC_demo_channel&segment=OLD_SILENT"
        )

        self.assertEqual(filtered.status_code, 200)
        self.assertIn("長期サイレント", filtered.text)
        self.assertNotIn("アクティブ</td>", filtered.text)

    def test_csv_export_is_downloadable(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.post(f"/connections/{connection_id}/collect")

        export = self.post(
            "/analysis/export",
            {"channel_id": "UC_demo_channel", "never_commented": "false"},
        )

        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment;", export.headers["content-disposition"])
        self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("subscriber_channel_id", export.text)

    def test_disconnecting_keeps_the_collected_data(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.post(f"/connections/{connection_id}/collect")

        response = self.post(f"/connections/{connection_id}/disconnect")

        self.assertEqual(response.status_code, 303)
        self.assertIn("msg=disconnected", response.headers["location"])
        analysis = self.client.get("/analysis?channel_id=UC_demo_channel")
        self.assertEqual(analysis.status_code, 200)


class SafetyTests(WebFixture):
    def test_no_page_ever_renders_a_credential_or_state_value(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.post(f"/connections/{connection_id}/collect")

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


if __name__ == "__main__":
    unittest.main()
