from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from workspace_access.models import AccessSecret

from web_ui.app import COLLECT_LIMIT_PER_MINUTE, WRITE_LIMIT_PER_MINUTE, create_app
from web_ui.container import (
    FileStateStore,
    build_services,
    google_config_from_env,
    state_dir_from_env,
)
from web_ui.google_provider import GoogleOAuthConfig
from web_ui.tests.test_google_provider import FakeTransport, default_replies


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

    def test_the_policy_allows_the_google_consent_screen_only(self) -> None:
        response = self.client.get("/login")

        directive = [
            part
            for part in response.headers["Content-Security-Policy"].split(";")
            if "form-action" in part
        ]

        self.assertEqual(directive, [" form-action 'self' https://accounts.google.com"])


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

    def test_the_run_history_shows_japanese_labels_only(self) -> None:
        self.login()
        self.create_workspace()
        self.connect_channel()
        connection_id = self.client.get("/").text.split("/connections/")[1].split("/collect")[0]
        self.post(f"/connections/{connection_id}/collect")

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
        self.post(f"/connections/{connection_id}/collect")

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

        collected = self.client.post(
            f"/connections/{connection_id}/collect",
            data={"csrf_token": self.client.cookies["yna_csrf"]},
        )

        self.assertEqual(collected.status_code, 303)
        self.assertIn("msg=collected", collected.headers["location"])
        self.assertTrue(
            any("/subscriptions?" in request[1] for request in self.transport.requests)
        )


class ProviderConfigurationTests(unittest.TestCase):
    ENVIRONMENT = {
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


if __name__ == "__main__":
    unittest.main()
