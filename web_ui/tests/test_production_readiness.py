"""Regressions for production entry points; no live provider calls."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib
import importlib.util
import json
import os
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from collection_jobs.models import EnqueueRun, RunKind, RunStatus
from web_ui.app import create_app
from web_ui.tests.test_web import WebFixture
from workspace_access.models import AccessSecret, Permission, SessionEvidence, WorkspaceSelection


class CollectionResumeTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()
        self.create_workspace()
        self.connect_channel()
        page = self.client.get('/')
        self.connection_id = re.search(r'/connections/([^/\"<>]+)/collect', page.text).group(1)
        session = self.services.access.authenticate_session(
            SessionEvidence(secret=AccessSecret(self.client.cookies['yna_session'])))
        self.context = self.services.access.resolve_workspace_context(
            session, WorkspaceSelection(workspace_id=self.client.cookies['yna_workspace']),
            Permission.COLLECTION_RUN)

    def runs(self):
        return self.services.jobs.list_runs(self.context).items

    def start(self):
        return self.post(f'/connections/{self.connection_id}/collect')

    def test_double_click_resumes_without_creating_or_spending(self):
        self.assertEqual(self.start().status_code, 303)
        before = self.runs()
        response = self.start()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/collecting')
        self.assertEqual(self.runs(), before)
        self.assertEqual(len(before), 2)
        self.assertTrue(all(run.quota_spent == 0 for run in before))

    def test_content_wait_does_not_start_another_subscriber_collection(self):
        self.services.jobs.enqueue_run(self.context, EnqueueRun(
            connection_id=self.connection_id, kind=RunKind.OWNER_CONTENT,
            idempotency_key='content-already-active'))
        before = self.runs()
        response = self.start()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.runs(), before)

    def test_quota_wait_keeps_run_identity_and_retry_time(self):
        self.assertEqual(self.start().status_code, 303)
        state = self.services.jobs._state
        for key, run in tuple(state.runs.items()):
            state.runs[key] = replace(run, next_attempt_at=datetime.now(UTC) + timedelta(days=2))
        before = self.runs()
        self.assertEqual(self.start().status_code, 303)
        self.assertEqual(self.runs(), before)

    def test_active_run_is_not_hidden_by_a_large_terminal_history(self):
        self.services.jobs.enqueue_run(self.context, EnqueueRun(
            connection_id=self.connection_id, kind=RunKind.OWNER_CONTENT,
            idempotency_key='old-active'))
        active = self.runs()[0]
        for i in range(2100):
            done = replace(active, run_id=f'finished_{i}', status=RunStatus.SUCCEEDED,
                           enqueued_at=active.enqueued_at + timedelta(seconds=i + 1),
                           finished_at=active.enqueued_at + timedelta(seconds=i + 2))
            self.services.jobs._state.runs[f'{active.workspace_id}\x1f{done.run_id}'] = done
        self.assertEqual(self.client.get('/collecting').status_code, 200)
        before = len(self.services.jobs._state.runs)
        self.assertEqual(self.start().status_code, 303)
        self.assertEqual(len(self.services.jobs._state.runs), before)

    def test_resume_still_requires_csrf(self):
        self.start()
        before = self.runs()
        response = self.client.post(f'/connections/{self.connection_id}/collect')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.runs(), before)

    def test_unknown_connection_is_not_reported_as_resumed(self):
        response = self.post('/connections/missing/collect')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.runs(), ())


class RuntimeTests(unittest.TestCase):
    def runtime(self):
        self.assertIsNotNone(importlib.util.find_spec('web_ui.runtime'))
        return importlib.import_module('web_ui.runtime')

    def environment(self):
        return {
            'K_SERVICE': 'yna-web', 'YNA_BASE_URL': 'https://app.example',
            'YNA_REQUIRE_GOOGLE': '1',
            'YNA_GOOGLE_CLIENT_ID': 'test.apps.googleusercontent.com',
            'YNA_GOOGLE_CLIENT_SECRET': 'private-client-value',
            'YNA_FIRESTORE_DATABASE': 'projects/test-project/databases/(default)',
            'YNA_CREDENTIAL_SECRET': 'projects/test-project/secrets/owner-tokens',
            'YNA_YOUTUBE_API_KEY': 'private-api-value',
            'YNA_DRAIN_SERVICE_ACCOUNT': 'scheduler@test-project.iam.gserviceaccount.com',
        }

    def test_local_demo_is_explicitly_supported(self):
        self.runtime().validate_production_environment({})

    def test_valid_cloud_run_configuration_is_accepted(self):
        self.runtime().validate_production_environment(self.environment())

    def test_production_missing_settings_fail_without_printing_secrets(self):
        runtime = self.runtime()
        for key in ('YNA_BASE_URL', 'YNA_REQUIRE_GOOGLE', 'YNA_GOOGLE_CLIENT_ID',
                    'YNA_GOOGLE_CLIENT_SECRET', 'YNA_FIRESTORE_DATABASE',
                    'YNA_CREDENTIAL_SECRET', 'YNA_YOUTUBE_API_KEY',
                    'YNA_DRAIN_SERVICE_ACCOUNT'):
            with self.subTest(key=key):
                environment = self.environment()
                del environment[key]
                with self.assertRaises(RuntimeError) as caught:
                    runtime.validate_production_environment(environment)
                self.assertIn(key, str(caught.exception))
                self.assertNotIn('private-client-value', str(caught.exception))
                self.assertNotIn('private-api-value', str(caught.exception))

    def test_unsafe_origins_ephemeral_disk_and_extra_workers_fail(self):
        runtime = self.runtime()
        cases = [
            ('YNA_BASE_URL', 'http://app.example'),
            ('YNA_BASE_URL', 'https://user:secret@app.example'),
            ('YNA_BASE_URL', 'https://app.example/path'),
            ('YNA_BASE_URL', 'https://app.example?key=secret'),
            ('YNA_BASE_URL', 'https://localhost'),
            ('YNA_STATE_DIR', '/tmp/state'),
            ('WEB_CONCURRENCY', '2'),
            ('YNA_FIRESTORE_DATABASE', 'not-a-resource'),
            ('YNA_CREDENTIAL_SECRET', 'not-a-resource'),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                environment = self.environment()
                environment[key] = value
                with self.assertRaises(RuntimeError):
                    runtime.validate_production_environment(environment)

    def test_health_is_read_only_and_identifies_initialization_scope(self):
        with patch.dict(os.environ, {'K_REVISION': 'test-revision'}, clear=True):
            app = create_app()
        with TestClient(app, base_url='https://testserver') as client:
            for path in ('/health/live', '/health/ready'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('set-cookie', response.headers)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(response.headers['x-yna-revision'], 'test-revision')
            body = client.get('/health/ready').json()
            self.assertEqual(body['scope'], 'application_initialization')
            self.assertEqual(body['mode'], 'demo')
            self.assertNotIn('private', json.dumps(body))

    def test_error_diagnostic_logs_only_code_and_route_template(self):
        self.runtime()
        client = TestClient(create_app(), base_url='https://testserver')
        with self.assertLogs('web_ui.runtime', level='WARNING') as logged:
            response = client.post('/connections/private-identifier/collect?code=oauth-secret',
                                   headers={'X-Request-ID': 'attacker-value'})
        self.assertEqual(response.status_code, 403)
        output = '\n'.join(logged.output)
        self.assertIn('CSRF', output)
        self.assertIn('/connections/{connection_id}/collect', output)
        for private in ('private-identifier', 'oauth-secret', 'attacker-value'):
            self.assertNotIn(private, output)
        self.assertRegex(response.headers['x-yna-request-id'], r'^[a-f0-9]{32}$')
