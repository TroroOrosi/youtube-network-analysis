"""Temporary reviewed patch; prove failures before fixing, then remove."""
from pathlib import Path
import subprocess
import sys


def replace_once(path, old, new):
    file = Path(path)
    text = file.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit('Source changed; refusing overwrite: ' + path)
    file.write_text(text.replace(old, new, 1), encoding='utf-8')


tests = Path('web_ui/tests/test_production_release.py')
with tests.open('a', encoding='utf-8') as file:
    file.write('''

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
''')
red = subprocess.run([sys.executable, '-m', 'unittest',
                      'web_ui.tests.test_production_release.ReleaseBoundaryTests', '-v'],
                     capture_output=True, text=True)
print(red.stdout + red.stderr)
if red.returncode != 1 or 'FAILED (failures=3)' not in red.stderr:
    raise SystemExit('Expected three reproduced boundary failures before applying fixes.')

replace_once('scripts/release_production.py',
    "        revision = service['status']['latestReadyRevisionName']\n",
    "        revision = service['status']['latestReadyRevisionName']\n"
    "        require(service['status'].get('latestCreatedRevisionName', revision) == revision,\n"
    "                'A newer revision is not ready; inspect its configuration before releasing.')\n")
replace_once('scripts/release_production.py',
    "            'scheduler_state': scheduler.get('state', 'UNKNOWN'),\n",
    "            'scheduler_state': scheduler.get('state', 'UNKNOWN'),\n"
    "            # Compare configuration, not volatile execution timestamps. This\n"
    "            # digest detects drift without printing secret-reference values.\n"
    "            'configuration_fingerprint': hashlib.sha256(json.dumps({\n"
    "                'spec': service['spec'],\n"
    "                'annotations': service['metadata'].get('annotations', {}),\n"
    "                'scheduler': {key: scheduler.get(key) for key in (\n"
    "                    'name', 'state', 'schedule', 'timeZone',\n"
    "                    'httpTarget', 'attemptDeadline', 'retryConfig')},\n"
    "            }, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),\n")
replace_once('scripts/release_production.py',
    "        # Ctrl-C during activation fails closed, rather than rolling back data.\n"
    "        try:\n",
    "        # Ctrl-C during activation fails closed, rather than rolling back data.\n"
    "        # A resume request can succeed remotely before its response is lost.\n"
    "        # Re-pause it on every failure, then stop the service independently.\n"
    "        try:\n"
    "            parts = checked['scheduler_name'].split('/')\n"
    "            command('scheduler', 'jobs', 'pause', parts[5],\n"
    "                    '--project=' + parts[1], '--location=' + parts[3])\n"
    "        except Exception:\n"
    "            print('WARNING: Scheduler pause could not be confirmed; inspect it immediately.')\n"
    "        try:\n")
replace_once('README.md', '# YouTube オーディエンス・ネットワーク分析\n',
    '# YouTube オーディエンス・ネットワーク分析\n\n'
    '> **本番運用：** 更新・復旧は [本番更新手順](docs/operations/production-release.md) を使用してください。\n'
    '> 既存データの退避、全 writer 停止、稼働コミットの照合、所有者による収集確認が必要です。\n'
    '> CI 合格だけでは本番デプロイ・実収集の完了を意味しません。\n')
replace_once('web_ui/README.md', '# web-ui\n',
    '# web-ui\n\n'
    '> **2026-09-18 production entry point:** Use\n'
    '> [the guarded production release runbook](../docs/operations/production-release.md)\n'
    '> for an existing Cloud Run service. Hosted startup now requires complete Google,\n'
    '> Firestore, credential-vault, API-key and Scheduler configuration.\n'
    '> The historical bootstrap commands below are not an existing-service update recipe:\n'
    '> do not replace existing environment/secrets with `--set-env-vars` or `--set-secrets`.\n'
    '> Local demo operation remains supported; an incomplete public Cloud Run demo does not.\n')
print('Applied three reproduced boundary fixes and current runbook entry points.')
