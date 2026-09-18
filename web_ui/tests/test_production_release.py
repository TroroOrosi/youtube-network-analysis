"""Release controller tests. Cloud calls are doubles, never production writes."""
from copy import deepcopy
import importlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


def service_fixture():
    values = {
        'YNA_BASE_URL': 'https://app.example', 'YNA_REQUIRE_GOOGLE': '1',
        'YNA_GOOGLE_CLIENT_ID': 'example.apps.googleusercontent.com',
        'YNA_FIRESTORE_DATABASE': 'projects/example/databases/(default)',
        'YNA_CREDENTIAL_SECRET': 'projects/example/secrets/owner-tokens',
        'YNA_DRAIN_SERVICE_ACCOUNT': 'scheduler@example.iam.gserviceaccount.com',
    }
    env = [{'name': k, 'value': v} for k, v in values.items()]
    for key in ('YNA_GOOGLE_CLIENT_SECRET', 'YNA_YOUTUBE_API_KEY'):
        env.append({'name': key, 'valueFrom': {'secretKeyRef': {'name': 'private-reference', 'key': 'latest'}}})
    return {
        'metadata': {'name': 'yna-web', 'annotations': {
            'run.googleapis.com/scalingMode': 'manual',
            'run.googleapis.com/manualInstanceCount': '0'}},
        'spec': {'template': {'spec': {'containers': [{'image': 'old-image', 'env': env}]}}},
        'status': {'latestReadyRevisionName': 'yna-web-old',
                   'traffic': [{'revisionName': 'yna-web-old', 'percent': 100}]},
    }


def scheduler_fixture():
    return [{'name': 'projects/example/locations/asia-northeast1/jobs/drain',
             'state': 'PAUSED', 'httpTarget': {
                 'uri': 'https://app.example/internal/drain', 'httpMethod': 'POST',
                 'oidcToken': {'serviceAccountEmail': 'scheduler@example.iam.gserviceaccount.com',
                               'audience': 'https://app.example/internal/drain'}}}]


class ReleaseTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.release_production'))
        return importlib.import_module('scripts.release_production')

    def inspect(self, service=None, jobs=None, **kwargs):
        return self.module().inspect_service(
            service or service_fixture(), scheduler_fixture() if jobs is None else jobs,
            **kwargs)

    def test_complete_stopped_configuration_is_accepted(self):
        checked = self.inspect(stopped=True, expected_revision='yna-web-old')
        self.assertEqual(checked['base_url'], 'https://app.example')
        self.assertEqual(checked['revision'], 'yna-web-old')
        self.assertEqual(checked['scheduler_name'], scheduler_fixture()[0]['name'])

    def test_partial_config_and_plaintext_secrets_are_rejected(self):
        module = self.module()
        for key in ('YNA_FIRESTORE_DATABASE', 'YNA_GOOGLE_CLIENT_SECRET', 'YNA_YOUTUBE_API_KEY'):
            service = service_fixture()
            env = service['spec']['template']['spec']['containers'][0]['env']
            env[:] = [item for item in env if item['name'] != key]
            with self.subTest(key=key), self.assertRaises(module.ReleaseError):
                self.inspect(service)
        service = service_fixture()
        for entry in service['spec']['template']['spec']['containers'][0]['env']:
            if entry['name'] == 'YNA_GOOGLE_CLIENT_SECRET':
                entry.clear()
                entry.update(name='YNA_GOOGLE_CLIENT_SECRET', value='secret-value')
        with self.assertRaises(module.ReleaseError) as caught:
            self.inspect(service)
        self.assertNotIn('secret-value', str(caught.exception))

    def test_wrong_revision_live_writers_tags_and_split_traffic_are_rejected(self):
        module = self.module()
        variants = []
        service = service_fixture()
        service['metadata']['annotations']['run.googleapis.com/manualInstanceCount'] = '1'
        variants.append(service)
        service = service_fixture()
        service['status']['traffic'][0]['tag'] = 'old-version'
        variants.append(service)
        service = service_fixture()
        service['status']['traffic'] = [
            {'revisionName': 'yna-web-old', 'percent': 50},
            {'revisionName': 'yna-web-other', 'percent': 50}]
        variants.append(service)
        service = service_fixture()
        service['spec']['template']['spec']['containers'][0]['command'] = ['custom-server']
        variants.append(service)
        for service in variants:
            with self.subTest(service=service), self.assertRaises(module.ReleaseError):
                self.inspect(service, stopped=True, expected_revision='yna-web-old')
        with self.assertRaises(module.ReleaseError):
            self.inspect(stopped=True, expected_revision='yna-web-stale')

    def test_missing_wrong_and_enabled_scheduler_are_rejected(self):
        module = self.module()
        with self.assertRaises(module.ReleaseError):
            self.inspect(jobs=[])
        for field, value in [('serviceAccountEmail', 'other@example.iam.gserviceaccount.com'),
                             ('audience', 'https://wrong.example')]:
            jobs = scheduler_fixture()
            jobs[0]['httpTarget']['oidcToken'][field] = value
            with self.subTest(field=field), self.assertRaises(module.ReleaseError):
                self.inspect(jobs=jobs)
        jobs = scheduler_fixture()
        jobs[0]['state'] = 'ENABLED'
        with self.assertRaises(module.ReleaseError):
            self.inspect(jobs=jobs, stopped=True)

    def test_release_preserves_environment_and_resumes_scheduler_only_after_probe(self):
        module = self.module()
        checked = self.inspect(stopped=True)
        calls = []
        def command(*args):
            calls.append(args)
            return {}
        def probe(*args):
            calls.append(('probe', *args))
        revision = module.deploy_prepared_source(
            checked, project='example', region='asia-northeast1', service='yna-web',
            source=Path('/tmp/clean-source'), source_sha='a' * 40, suffix='p-test',
            command=command, probe=probe)
        self.assertEqual(revision, 'yna-web-p-test')
        strings = [' '.join(map(str, c)) for c in calls]
        deploy = strings[0]
        self.assertIn('--no-traffic', deploy)
        self.assertIn('--scaling=0', deploy)
        for forbidden in ('--set-env-vars', '--set-secrets', '--allow-unauthenticated', '--to-latest'):
            self.assertNotIn(forbidden, '\n'.join(strings))
        probe_index = next(i for i, c in enumerate(calls) if c[0] == 'probe')
        resume_index = next(i for i, c in enumerate(calls) if c[:3] == ('scheduler', 'jobs', 'resume'))
        self.assertLess(probe_index, resume_index)
        self.assertIn('--to-revisions=yna-web-p-test=100', '\n'.join(strings))

    def test_failed_smoke_disables_service_and_never_rolls_back_or_resumes(self):
        module = self.module()
        calls = []
        def command(*args):
            calls.append(args)
            return {}
        def probe(*args):
            raise module.ReleaseError('probe failed')
        with self.assertRaises(module.ReleaseError):
            module.deploy_prepared_source(
                self.inspect(stopped=True), project='example', region='asia-northeast1',
                service='yna-web', source=Path('/tmp/source'), source_sha='b' * 40,
                suffix='p-failed', command=command, probe=probe)
        self.assertIn('--scaling=0', calls[-1])
        self.assertFalse(any(c[:3] == ('scheduler', 'jobs', 'resume') for c in calls))
        self.assertFalse(any('--to-revisions=yna-web-old=100' in c for c in calls))

    def test_backup_detects_changes_and_never_reads_credential_vault(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as parent:
            reads = []
            def load(name):
                reads.append(name)
                return '{"module":"' + name + '"}'
            target = Path(parent) / 'backup'
            module.backup_state(target, load)
            self.assertEqual(set(reads), set(module.STATE_MODULES))
            self.assertEqual(len(reads), 10)
            self.assertTrue((target / 'manifest.json').is_file())
            self.assertFalse(any('credential' in name for name in reads))
        with tempfile.TemporaryDirectory() as parent:
            reads = []
            def changing(name):
                reads.append(name)
                return '{}' if len(reads) <= 5 else '{"changed":true}'
            target = Path(parent) / 'changed'
            with self.assertRaises(module.ReleaseError):
                module.backup_state(target, changing)
            self.assertFalse((target / 'manifest.json').exists())


class ReleaseBoundaryTests(unittest.TestCase):
    def test_unready_newer_revision_is_not_mistaken_for_serving_config(self):
        from scripts import release_production as module
        service = service_fixture()
        service['status']['latestCreatedRevisionName'] = 'yna-web-unready'
        with self.assertRaises(module.ReleaseError):
            module.inspect_service(service, scheduler_fixture())

    def test_secret_reference_drift_changes_inspection_fingerprint(self):
        from scripts import release_production as module
        first = service_fixture()
        changed = deepcopy(first)
        env = changed['spec']['template']['spec']['containers'][0]['env']
        next(item for item in env if item['name'] == 'YNA_GOOGLE_CLIENT_SECRET')['valueFrom']['secretKeyRef']['key'] = '8'
        self.assertNotEqual(module.inspect_service(first, scheduler_fixture()),
                            module.inspect_service(changed, scheduler_fixture()))

    def test_resume_timeout_pauses_scheduler_before_disabling_service(self):
        from scripts import release_production as module
        checked = module.inspect_service(service_fixture(), scheduler_fixture(), stopped=True)
        calls = []
        def command(*args):
            calls.append(args)
            if args[:3] == ('scheduler', 'jobs', 'resume'):
                raise module.ReleaseError('resume may already have succeeded remotely')
            return {}
        with self.assertRaises(module.ReleaseError):
            module.deploy_prepared_source(
                checked, project='example', region='asia-northeast1', service='yna-web',
                source=Path('/tmp/source'), source_sha='c' * 40, suffix='p-timeout',
                command=command, probe=lambda *args: None)
        self.assertEqual(calls[-2][:3], ('scheduler', 'jobs', 'pause'))
        self.assertIn('--scaling=0', calls[-1])
