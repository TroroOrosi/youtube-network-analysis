"""Safe, recoverable responses during collection/storage outages."""
from unittest.mock import patch
from fastapi.testclient import TestClient
from web_ui.app import COLLECT_LIMIT_PER_MINUTE, create_app
from web_ui.gcp import GcpUnavailable
from web_ui.tests.test_web import BASE_URL, WebFixture


class AvailabilityTests(WebFixture):
    def test_storage_outage_is_safe_503_and_login_remains_available(self):
        self.client = TestClient(create_app(self.services), base_url=BASE_URL,
                                 follow_redirects=False, raise_server_exceptions=False)
        self.login()
        with patch.object(self.services.access, "authenticate_session",
                          side_effect=GcpUnavailable("synthetic-private-detail")):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "60")
            self.assertNotIn("synthetic-private-detail", response.text)
            self.assertIn("保存先", response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(self.client.get("/login").status_code, 200)

    def test_collection_rate_limit_tells_the_driver_when_to_retry(self):
        self.login()
        self.create_workspace()
        for _ in range(COLLECT_LIMIT_PER_MINUTE + 1):
            response = self.post("/collecting/step")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "60")
