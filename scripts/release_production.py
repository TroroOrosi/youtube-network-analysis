"""Fail-closed Cloud Shell release of an EXISTING single-writer service.

python -m scripts.release_production --project PROJECT --check
No deployment occurs without --apply, a pinned live revision and an explicit
operator attestation that every writer stopped. See the production runbook.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from web_ui.runtime import validate_production_environment

STATE_MODULES = (
    'workspace_access', 'channel_connections', 'channel_data',
    'collection_jobs', 'analysis_api',
)


class ReleaseError(RuntimeError):
    """Operator-safe error, without command output, credentials or state."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def inspect_service(service: dict, jobs: list, *, stopped: bool = False,
                    expected_revision: str | None = None) -> dict:
    """Validate captured configuration without accessing secret payloads."""
    try:
        containers = service['spec']['template']['spec']['containers']
        require(len(containers) == 1, 'Exactly one application container is supported.')
        container = containers[0]
        require(not container.get('command') and not container.get('args'),
                'Remove custom container command/args; the audited image supplies one worker.')
        environment = {'K_SERVICE': service['metadata']['name']}
        names = set()
        for entry in container.get('env', []):
            name = entry['name']
            require(name not in names, 'Duplicate environment variable names are unsupported.')
            names.add(name)
            reference = entry.get('valueFrom', {}).get('secretKeyRef', {})
            if name in ('YNA_GOOGLE_CLIENT_SECRET', 'YNA_YOUTUBE_API_KEY'):
                require(bool(reference.get('name')) and bool(reference.get('key')),
                        name + ' must use a Secret Manager reference, not a plaintext value.')
            environment[name] = '__secret_reference__' if reference else entry.get('value', '')
        validate_production_environment(environment)
        revision = service['status']['latestReadyRevisionName']
        require(service['status'].get('latestCreatedRevisionName', revision) == revision,
                'A newer revision is not ready; inspect its configuration before releasing.')
        require(bool(re.fullmatch(r'[a-z0-9-]{1,128}', revision)), 'Invalid ready revision.')
        if expected_revision is not None:
            require(revision == expected_revision, 'Live revision changed; inspect again.')
        traffic = service['status'].get('traffic', [])
        routed = [item for item in traffic if int(item.get('percent', 0)) > 0]
        require(len(routed) == 1 and int(routed[0]['percent']) == 100
                and routed[0].get('revisionName') == revision,
                'Traffic must target the single expected ready revision; no split/canary.')
        base = environment['YNA_BASE_URL'].rstrip('/')
        drain_url = base + '/internal/drain'
        matches = [job for job in jobs if job.get('httpTarget', {}).get('uri') == drain_url]
        require(len(matches) == 1, 'Exactly one Scheduler job must target the configured drain URL.')
        scheduler = matches[0]
        target = scheduler['httpTarget']
        oidc = target.get('oidcToken', {})
        require(target.get('httpMethod') == 'POST', 'Scheduler must use POST.')
        require(oidc.get('serviceAccountEmail') == environment['YNA_DRAIN_SERVICE_ACCOUNT'],
                'Scheduler OIDC service account does not match application configuration.')
        audience = environment.get('YNA_DRAIN_AUDIENCE', '').strip() or drain_url
        require((oidc.get('audience') or drain_url) == audience,
                'Scheduler OIDC audience does not match application configuration.')
        require(bool(re.fullmatch(r'projects/[^/]+/locations/[^/]+/jobs/[^/]+', scheduler['name'])),
                'Unsupported Scheduler resource name.')
        if stopped:
            annotations = service['metadata'].get('annotations', {})
            require(annotations.get('run.googleapis.com/scalingMode') == 'manual'
                    and str(annotations.get('run.googleapis.com/manualInstanceCount')) == '0',
                    'Disable the service with manual scaling=0 before applying.')
            require(not any(item.get('tag') for item in traffic + service['spec'].get('traffic', [])),
                    'Remove all revision traffic tags before applying; tagged URLs bypass scaling=0.')
            require(scheduler.get('state') == 'PAUSED', 'Pause the Scheduler job before applying.')
        return {
            'base_url': base, 'revision': revision,
            'database': environment['YNA_FIRESTORE_DATABASE'],
            'scheduler_name': scheduler['name'],
            'scheduler_state': scheduler.get('state', 'UNKNOWN'),
            # Compare configuration, not volatile execution timestamps. This
            # digest detects drift without printing secret-reference values.
            'configuration_fingerprint': hashlib.sha256(json.dumps({
                'spec': service['spec'],
                'annotations': service['metadata'].get('annotations', {}),
                'scheduler': {key: scheduler.get(key) for key in (
                    'name', 'state', 'schedule', 'timeZone',
                    'httpTarget', 'attemptDeadline', 'retryConfig')},
            }, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        }
    except ReleaseError:
        raise
    except RuntimeError as error:
        raise ReleaseError(str(error)) from None
    except (KeyError, TypeError, ValueError):
        raise ReleaseError('Unsupported Cloud Run or Scheduler configuration; no changes made.') from None


def _run(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError('Operator command could not finish; inspect the Cloud console.') from None
    require(result.returncode == 0,
            'Operator command failed; inspect the Cloud console. Raw output was not copied.')
    return result.stdout.strip()


def gcloud(*args: str) -> dict | list:
    text = _run('gcloud', *map(str, args), '--quiet', '--format=json')
    try:
        return json.loads(text) if text else {}
    except ValueError:
        raise ReleaseError('gcloud returned unreadable JSON; inspect the Cloud console.') from None


def _private_write(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def backup_state(directory: Path, load) -> None:
    """Back up all five logical documents; verify stability before completion.

    No Secret Manager payload is fetched. These files STILL contain sensitive
    account/session metadata: keep them outside the repository, mode 0700/0600.
    """
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    digests = {}
    for module in STATE_MODULES:
        document = load(module)
        require(document is None or isinstance(document, str), 'Unsupported state document.')
        digests[module] = None if document is None else hashlib.sha256(document.encode()).hexdigest()
        if document is not None:
            _private_write(directory / (module + '.json'), document)
    for module in STATE_MODULES:
        document = load(module)
        digest = None if document is None else hashlib.sha256(document.encode()).hexdigest()
        require(digest == digests[module], 'State changed during backup; another writer may be active.')
    _private_write(directory / 'manifest.json', json.dumps({
        'format': 1, 'created_at': datetime.now(UTC).isoformat(),
        'sha256': digests, 'credential_payloads_included': False,
    }, indent=2) + '\n')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def smoke_probe(base: str, source_sha: str, revision: str) -> None:
    """Read-only smoke plus a credential-free, denied POST to the drain."""
    opener = build_opener(_NoRedirect)
    def request(path, method='GET'):
        req = Request(base + path, method=method, headers={'User-Agent': 'YNA-release-smoke'})
        try:
            reply = opener.open(req, timeout=20)
        except HTTPError as error:
            reply = error
        with reply:
            return reply.code, reply.headers, reply.read(65536)
    for attempt in range(20):
        try:
            status, _, payload = request('/health/ready')
            body = json.loads(payload)
            if (status == 200 and isinstance(body, dict) and body.get('status') == 'ready'
                    and body.get('source_revision') == source_sha
                    and body.get('revision') == revision and body.get('mode') == 'google'):
                break
        except (OSError, URLError, ValueError):
            pass
        if attempt == 19:
            raise ReleaseError('Source/revision readiness probe failed; not a verified release.')
        time.sleep(3)
    try:
        status, headers, _ = request('/login')
        require(status == 200 and headers.get('Cache-Control') == 'no-store',
                'Login smoke failed.')
        status, headers, _ = request('/')
        require(status == 303 and headers.get('Location') == '/login',
                'Unauthenticated home access was not denied.')
        status, _, _ = request('/internal/drain', 'POST')
        require(status == 401, 'Unauthenticated drain request was not denied.')
    except (OSError, URLError):
        raise ReleaseError('Network failure during smoke verification.') from None


def deploy_prepared_source(checked: dict, *, project: str, region: str, service: str,
                           source: Path, source_sha: str, suffix: str,
                           command=gcloud, probe=smoke_probe) -> str:
    """Deploy only AFTER caller verified quiescence, backup and clean source."""
    revision = service + '-' + suffix
    common = ('--project=' + project, '--region=' + region)
    command('run', 'deploy', service, '--source=' + str(source), *common,
            '--revision-suffix=' + suffix, '--no-traffic', '--scaling=0',
            '--min-instances=0', '--max-instances=1', '--timeout=300',
            '--update-labels=yna-source=' + source_sha)
    command('run', 'services', 'update-traffic', service, *common,
            '--to-revisions=' + revision + '=100', '--clear-tags')
    try:
        command('run', 'services', 'update', service, *common, '--scaling=1')
        probe(checked['base_url'], source_sha, revision)
        parts = checked['scheduler_name'].split('/')
        command('scheduler', 'jobs', 'resume', parts[5],
                '--project=' + parts[1], '--location=' + parts[3])
    except BaseException:
        # Never switch an old binary onto a possibly migrated snapshot. Even
        # Ctrl-C during activation fails closed, rather than rolling back data.
        # A resume request can succeed remotely before its response is lost.
        # Re-pause it on every failure, then stop the service independently.
        try:
            parts = checked['scheduler_name'].split('/')
            command('scheduler', 'jobs', 'pause', parts[5],
                    '--project=' + parts[1], '--location=' + parts[3])
        except Exception:
            print('WARNING: Scheduler pause could not be confirmed; inspect it immediately.')
        try:
            command('run', 'services', 'update', service, *common, '--scaling=0')
        except Exception:
            print('WARNING: service disable could not be confirmed; inspect Cloud Run immediately.')
        raise
    return revision


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', default='asia-northeast1')
    parser.add_argument('--service', default='yna-web')
    parser.add_argument('--scheduler-location')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='read-only, the default')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-revision')
    parser.add_argument('--writers-stopped', action='store_true')
    parser.add_argument('--backup-directory', type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    for value in (args.project, args.region, args.service, args.scheduler_location or args.region):
        require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', value)), 'Invalid resource identifier.')
    common = ('--project=' + args.project, '--region=' + args.region)
    def inspect():
        return inspect_service(
            gcloud('run', 'services', 'describe', args.service, *common),
            gcloud('scheduler', 'jobs', 'list', '--project=' + args.project,
                   '--location=' + (args.scheduler_location or args.region)),
            stopped=args.apply, expected_revision=args.expected_revision)
    if args.apply:
        require(args.writers_stopped and bool(args.expected_revision) and args.backup_directory is not None,
                '--apply requires --writers-stopped, --expected-revision and --backup-directory.')
    checked = inspect()
    if not args.apply:
        print(json.dumps({'status': 'CONFIGURATION_CHECKED_ONLY', **checked}, indent=2))
        print('No deployment, secret access, collection, or storage write was performed.')
        return 0
    root = Path(__file__).resolve().parents[1]
    require(not _run('git', 'status', '--porcelain', '--untracked-files=all', cwd=root),
            'Use a clean checkout. Do not put state backups in the repository.')
    source_sha = _run('git', 'rev-parse', 'HEAD', cwd=root)
    require(bool(re.fullmatch(r'[a-f0-9]{40}', source_sha)), 'Cannot pin the source revision.')
    backup = args.backup_directory.expanduser().resolve()
    require(not backup.is_relative_to(root), 'Backups must be outside the repository/build context.')
    from web_ui.gcp import FirestoreStateStore
    token = _run('gcloud', 'auth', 'print-access-token')
    require(bool(token), 'No operator access token is available.')
    backup_state(backup, lambda name: FirestoreStateStore(checked['database'], name, lambda: token).load())
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    require(all(manifest['sha256'][name] is not None for name in STATE_MODULES if name != 'analysis_api'),
            'Expected existing application state is missing; do not deploy onto an empty/wrong database.')
    require(inspect() == checked, 'Deployment configuration changed during backup; stop and inspect.')
    with tempfile.TemporaryDirectory(prefix='yna-release-') as directory:
        directory = Path(directory)
        archive = directory / 'source.tar'
        source = directory / 'source'
        source.mkdir()
        _run('git', 'archive', '--format=tar', '--output=' + str(archive), source_sha, cwd=root)
        with tarfile.open(archive) as files:
            for member in files.getmembers():
                require(member.isfile() or member.isdir(), 'Source archives must not contain links or devices.')
                require(not member.name.startswith('/') and '..' not in Path(member.name).parts,
                        'Unsafe source archive path.')
            files.extractall(source, filter='data')
        (source / 'web_ui' / 'build_info.py').write_text(
            '# Immutable image source identity; generated from a clean Git archive.\n'
            + 'SOURCE_REVISION = ' + repr(source_sha) + '\n', encoding='utf-8')
        suffix = 'p' + source_sha[:10] + '-' + datetime.now(UTC).strftime('%H%M%S')
        revision = deploy_prepared_source(
            checked, project=args.project, region=args.region, service=args.service,
            source=source, source_sha=source_sha, suffix=suffix)
    report = {
        'status': 'SOURCE_AND_PUBLIC_SMOKE_VERIFIED', 'source_revision': source_sha,
        'revision': revision, 'scheduler': 'resumed',
        'authenticated_collection': 'NOT_VERIFIED',
    }
    _private_write(backup / 'release-result.json', json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ReleaseError as error:
        print('STOP: ' + str(error))
        raise SystemExit(1) from None
    except Exception:
        print('STOP: release could not finish. Keep writers stopped; inspect local backup and Cloud console.')
        raise SystemExit(1) from None
