"""Quota waits show useful state without promising an unconfigured scheduler."""
from datetime import UTC, datetime
import unittest

from web_ui.app import _display_datetime
from collection_jobs.service import _next_utc_day
from web_ui.tests import test_web as helpers


class MultiDayUiTests(helpers.WebFixture):
    def prepare(self):
        self.login()
        self.create_workspace()
        self.connect_channel()
        return self.client.get('/').text.split('/connections/')[1].split('/collect')[0]

    def test_waiting_dashboard_names_reason_progress_time_and_manual_resume(self):
        connection = self.prepare()
        self.services.jobs._daily_quota_units = 6
        self.post(f'/connections/{connection}/collect')
        location = helpers.drive_collection(self, self.client)
        page = self.client.get(location)
        self.assertEqual(page.status_code, 200)
        for text in ('取得済みページ数', '次回実行可能時刻', '利用枠の回復待ち',
                     '自動実行が設定されている場合', '取得済みデータを保持'):
            self.assertIn(text, page.text)
        self.assertIn(_display_datetime(_next_utc_day(datetime.now(UTC)))[1], page.text)
        self.assertIn('href="/collecting"', page.text)
        self.assertNotIn('翌日以降に自動で再開します', page.text)

    def test_analysis_discloses_provider_limit_not_full_subscriber_coverage(self):
        connection = self.prepare()
        self.collect_now(connection)
        html = self.client.get('/').text
        url = html.split('href="/analysis?')[1].split('"')[0]
        page = self.client.get('/analysis?'+url)
        self.assertEqual(page.status_code, 200)
        self.assertIn('YouTube APIが返した公開登録者', page.text)
        self.assertIn('全登録者を取得できたことは保証しません', page.text)
