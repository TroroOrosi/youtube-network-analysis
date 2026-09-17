"""Operator-only, jobs-only recovery. Standalone Python 3.11+ / Cloud Shell.

Default: inspect and plan. --apply queues ONE content replacement; optional
--include-subscribers also queues ONE fresh subscriber traversal. No YouTube calls. Requires separately verified patched code, drained writers and a
manually disabled Cloud Run service. No session or credential is manufactured.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

MAX_STATE_BYTES = 900_000
MAX_RESPONSE_BYTES = 4_000_000
TARGET_KEYS = ('project', 'region', 'service', 'workspace_id', 'channel_id',
               'connection_id', 'source_run_id', 'owner_user_id')


class RecoveryError(RuntimeError):
    """A safe, actionable error; never include provider bodies or secrets."""


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON field; refusing ambiguous data.')
        result[key] = value
    return result


def parse_json(text):
    try:
        return json.loads(text, object_pairs_hook=_unique)
    except (ValueError, TypeError) as error:
        raise RecoveryError('Unreadable JSON; no changes made.') from error


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def validate_target(target):
    require(isinstance(target, dict) and set(target) == set(TARGET_KEYS),
            'Target must contain exactly the eight documented identifiers.')
    for key, value in target.items():
        require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value),
                'Invalid target identifier: ' + key)


def record(value, name):
    require(isinstance(value, dict) and value.get('#') == 'obj'
            and value.get('t') == name and isinstance(value.get('f'), dict),
            'Unsupported record shape: ' + name)
    return value['f']


def pairs(value):
    require(isinstance(value, dict) and value.get('#') == 'map'
            and isinstance(value.get('v'), list), 'Unsupported map shape.')
    seen = set()
    for pair in value['v']:
        require(isinstance(pair, list) and len(pair) == 2, 'Invalid map entry.')
        fingerprint = canonical(pair[0])
        require(fingerprint not in seen, 'Duplicate map key; refusing ambiguous data.')
        seen.add(fingerprint)
    return value['v']


def lookup(value, key):
    return next((v for k, v in pairs(value) if k == key), None)


def enum_value(value, kind):
    require(isinstance(value, dict) and value.get('#') == 'enum'
            and value.get('t') == kind and isinstance(value.get('v'), str),
            'Unsupported enum: ' + kind)
    return value['v']


def enum(kind, value):
    return {'#': 'enum', 't': kind, 'v': value}


def moment(value):
    require(isinstance(value, dict) and value.get('#') == 'time', 'Missing timestamp.')
    try:
        result = datetime.fromisoformat(value['v'])
        require(result.utcoffset() is not None, 'Timestamp must include a timezone.')
        return result.astimezone(UTC)
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryError('Invalid timestamp.') from error


def state(payload, versions):
    require(isinstance(payload, dict) and payload.get('version') in versions,
            'Unsupported snapshot version; no changes made.')
    return record(payload.get('state'), 'MemoryState')


def _matches(run, target):
    return (run.get('workspace_id') == target['workspace_id']
            and run.get('connection_id') == target['connection_id']
            and run.get('provider_channel_id') == target['channel_id']
            and enum_value(run.get('kind'), 'RunKind') == 'OWNER_CONTENT')


def _prepare_content_recovery(jobs, connections, access, target, now):
    """Pure transition. Never mutates input or subscriber data. No I/O."""
    validate_target(target)
    require(now.utcoffset() is not None, 'Recovery time must have a timezone.')
    jf = state(jobs, (1, 2))
    cf = state(connections, (1,))
    require(isinstance(access, dict) and access.get('version') == 1,
            'Unsupported workspace snapshot.')
    ws, source = target['workspace_id'], target['source_run_id']
    owner = [u for u in access.get('users', []) if u.get('user_id') == target['owner_user_id']]
    require(len(owner) == 1 and owner[0].get('enabled') is True
            and owner[0].get('deleted_at') is None, 'Expected owner is not active.')
    require(sum(w.get('workspace_id') == ws for w in access.get('workspaces', [])) == 1,
            'Expected workspace does not exist uniquely.')
    member = [m for m in access.get('memberships', [])
              if m.get('workspace_id') == ws and m.get('user_id') == target['owner_user_id']]
    require(len(member) == 1 and member[0].get('role') == 'OWNER',
            'Expected owner membership is absent; permissions will not be changed.')
    cx = record(lookup(cf.get('connections'), ws + '\x1f' + target['connection_id']),
                'ChannelConnection')
    require(all(cx.get(k) == target[k] for k in ('workspace_id', 'connection_id'))
            and cx.get('provider_channel_id') == target['channel_id']
            and enum_value(cx.get('provider'), 'ConnectionProvider') == 'YOUTUBE',
            'Connection identity does not match the requested target.')
    require(enum_value(cx.get('status'), 'ConnectionStatus') == 'ACTIVE',
            'Connection requires owner reauthorization; do not rewrite its status.')
    run_key = ws + '\x1f' + source
    old = record(lookup(jf.get('runs'), run_key), 'CollectionRun')
    require(old.get('run_id') == source and _matches(old, target),
            'Source run identity/kind does not match the target.')
    replacement = 'run_recovery_' + hashlib.sha256(
        canonical([ws, target['connection_id'], target['channel_id'], source]).encode()
    ).hexdigest()[:32]
    report = {'source_run_id': source, 'replacement_run_id': replacement,
              'workspace_id': ws, 'channel_id': target['channel_id'],
              'source_status': enum_value(old.get('status'), 'RunStatus'),
              'kind': 'OWNER_CONTENT', 'subscriber_recollection': False,
              'oauth_validity': 'NOT_CHECKED', 'writes': ['state/collection_jobs']}
    existing = lookup(jf['runs'], ws + '\x1f' + replacement)
    if existing is not None:
        existing = record(existing, 'CollectionRun')
        require(existing.get('run_id') == replacement and _matches(existing, target),
                'Replacement ID collision; no changes made.')
        report.update(outcome='ALREADY_PREPARED', writes=[],
                      replacement_status=enum_value(existing.get('status'), 'RunStatus'))
        return None, report
    allowed = report['source_status'] == 'RUNNING' or (
        report['source_status'] == 'FAILED'
        and enum_value(old.get('failure_reason'), 'RunFailureReason') == 'UNEXPECTED_FAILURE')
    require(allowed, 'Only an orphan RUNNING or startup-failed unexpected run can be recovered.')
    enqueued, started = moment(old.get('enqueued_at')), moment(old.get('started_at'))
    require(enqueued <= started <= now - timedelta(hours=1),
            'Run is recent or timestamps are inconsistent; refusing recovery.')
    for field in ('resume', 'page_checkpoints'):
        if field in jf:
            require(lookup(jf[field], run_key) is None,
                    'Run has a retained checkpoint; use the normal resume path.')
    for key, value in pairs(jf['runs']):
        other = record(value, 'CollectionRun')
        if key != run_key and _matches(other, target):
            require(enum_value(other.get('status'), 'RunStatus') not in ('QUEUED', 'RUNNING')
                    and moment(other.get('enqueued_at')) < enqueued,
                    'Another active or newer content run exists; inspect it first.')
    updated = deepcopy(jobs)
    fields = updated['state']['f']
    retired = lookup(fields['runs'], run_key)['f']
    time_value = {'#': 'time', 'v': now.astimezone(UTC).isoformat()}
    if report['source_status'] == 'RUNNING':
        retired.update(status=enum('RunStatus', 'FAILED'), finished_at=time_value,
                       failure_reason=enum('RunFailureReason', 'UNEXPECTED_FAILURE'),
                       next_attempt_at=None)
    fresh = {'#': 'obj', 't': 'CollectionRun', 'f': {
        'run_id': replacement, 'workspace_id': ws, 'connection_id': target['connection_id'],
        'provider_channel_id': target['channel_id'], 'kind': enum('RunKind', 'OWNER_CONTENT'),
        'status': enum('RunStatus', 'QUEUED'), 'attempt': 1, 'enqueued_at': time_value,
        'started_at': None, 'finished_at': None, 'pages_fetched': 0, 'quota_spent': 0,
        'failure_reason': None, 'next_attempt_at': None}}
    fields['runs']['v'].append([ws + '\x1f' + replacement, fresh])
    revisions = pairs(fields.get('revisions'))
    revision = lookup(fields['revisions'], ws)
    require(type(revision) is int and revision >= 0, 'Invalid workspace jobs revision.')
    for pair in revisions:
        if pair[0] == ws:
            pair[1] += 1
    require(len(canonical(updated).encode()) <= MAX_STATE_BYTES,
            'Repaired jobs document would require chunking; use a reviewed migration.')
    report['outcome'] = 'READY_TO_APPLY'
    return updated, report


def prepare_recovery(jobs, connections, access, target, now, *, include_subscribers=False):
    """Plan a content recovery, optionally adding one explicit subscriber refresh.

    The same live ownership/connection validation applies to both operations.
    Old successful subscriber runs and all accepted data remain untouched.
    A completed 1,000-row traversal has no next cursor to resume: this optional
    fresh run begins at page one, using the newly deployed mySubscribers feed.
    """
    updated, report = _prepare_content_recovery(jobs, connections, access, target, now)
    if not include_subscribers:
        return updated, report
    current = updated if updated is not None else jobs
    fields = state(current, (1, 2))
    ws = target['workspace_id']
    subscriber_id = 'run_recovery_sub_' + hashlib.sha256(canonical([
        ws, target['connection_id'], target['channel_id'], target['source_run_id'],
        'SUBSCRIBERS',
    ]).encode()).hexdigest()[:32]
    key = ws + '\x1f' + subscriber_id
    report.update(subscriber_recollection=True, subscriber_run_id=subscriber_id,
                  subscriber_query='mySubscribers', provider_result_limit='NOT_REMOVABLE')
    existing = lookup(fields['runs'], key)
    if existing is not None:
        fresh = record(existing, 'CollectionRun')
        require(fresh.get('run_id') == subscriber_id
                and all(fresh.get(k) == target[k] for k in ('workspace_id', 'connection_id'))
                and fresh.get('provider_channel_id') == target['channel_id']
                and enum_value(fresh.get('kind'), 'RunKind') == 'SUBSCRIBERS',
                'Subscriber replacement ID collision; no changes made.')
        report['subscriber_status'] = enum_value(fresh.get('status'), 'RunStatus')
        return updated, report
    source = record(lookup(fields['runs'], ws + '\x1f' + target['source_run_id']), 'CollectionRun')
    for _, value in pairs(fields['runs']):
        other = record(value, 'CollectionRun')
        same_channel = (other.get('workspace_id') == ws
                        and other.get('provider_channel_id') == target['channel_id']
                        and enum_value(other.get('kind'), 'RunKind') == 'SUBSCRIBERS')
        if same_channel:
            require(enum_value(other.get('status'), 'RunStatus') not in ('QUEUED', 'RUNNING')
                    and moment(other.get('enqueued_at')) <= moment(source.get('enqueued_at')),
                    'Another active or newer subscriber run exists; inspect it before refreshing.')
    updated = deepcopy(current)
    fields = updated['state']['f']
    fields['runs']['v'].append([key, {'#': 'obj', 't': 'CollectionRun', 'f': {
        'run_id': subscriber_id, 'workspace_id': ws, 'connection_id': target['connection_id'],
        'provider_channel_id': target['channel_id'], 'kind': enum('RunKind', 'SUBSCRIBERS'),
        'status': enum('RunStatus', 'QUEUED'), 'attempt': 1,
        'enqueued_at': {'#': 'time', 'v': now.astimezone(UTC).isoformat()},
        'started_at': None, 'finished_at': None, 'pages_fetched': 0, 'quota_spent': 0,
        'failure_reason': None, 'next_attempt_at': None,
    }}])
    revision = lookup(fields.get('revisions'), ws)
    require(type(revision) is int and revision >= 0, 'Invalid workspace jobs revision.')
    for pair in pairs(fields['revisions']):
        if pair[0] == ws:
            pair[1] += 1
    require(len(canonical(updated).encode()) <= MAX_STATE_BYTES,
            'Repaired jobs document would require chunking; use a reviewed migration.')
    report.update(outcome='READY_TO_APPLY', subscriber_status='QUEUED', writes=['state/collection_jobs'])
    return updated, report


def read_snapshot(head):
    require(isinstance(head, dict) and isinstance(head.get('fields'), dict),
            'Required state document is missing.')
    fields = head['fields']
    require('generation' not in fields and fields.get('parts', {'integerValue': '1'})
            == {'integerValue': '1'}, 'Chunked state is not supported by this narrow repair tool.')
    text = fields.get('document', {}).get('stringValue')
    require(isinstance(text, str) and len(text.encode()) <= MAX_STATE_BYTES,
            'State document is missing or exceeds the single-document limit.')
    return parse_json(text)


def serving_revision(service):
    statuses = service.get('trafficStatuses', [])
    require(len(statuses) == 1 and statuses[0].get('percent') == 100
            and isinstance(statuses[0].get('revision'), str),
            'Expected one revision serving 100% of traffic.')
    return statuses[0]['revision'].rsplit('/', 1)[-1]


def verify_disabled(service, expected_revision, writers_stopped):
    require(writers_stopped, 'Confirm all writers and in-flight requests have stopped first.')
    scaling = service.get('scaling', {})
    require(scaling.get('scalingMode') == 'MANUAL'
            and scaling.get('manualInstanceCount', 0) == 0,
            'Cloud Run must be disabled with manual scaling=0 before apply.')
    require(not service.get('reconciling', False)
            and service.get('observedGeneration') == service.get('generation')
            and service.get('generation') is not None, 'Cloud Run configuration is not settled.')
    require(not any(t.get('tag') for t in service.get('traffic', []) + service.get('trafficStatuses', [])),
            'Traffic-tag URLs can keep old writers alive; remove tags and drain them first.')
    require(serving_revision(service) == expected_revision,
            'Serving revision differs from the explicitly verified revision.')


def write_backup(directory, head, metadata):
    require(os.name == 'posix', 'Apply requires POSIX owner-only backup permissions (Cloud Shell).')
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not directory.is_symlink() and directory.is_dir()
            and directory.stat().st_uid == os.getuid(), 'Backup directory must be owned by this user.')
    os.chmod(directory, 0o700)
    path = directory / ('owner-content-' + uuid.uuid4().hex + '.json')
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        json.dump({'jobs_document': head, 'metadata': metadata}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    return path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RecoveryError('Unexpected redirect refused; no credential was forwarded.')


class Cloud:
    def __init__(self):
        self.opener = build_opener(_NoRedirect())

    def request(self, method, url, body=None):
        require(url.startswith(('https://run.googleapis.com/v2/', 'https://firestore.googleapis.com/v1/')),
                'API destination is not allowed.')
        try:
            token = subprocess.run(['gcloud', 'auth', 'print-access-token'], check=True,
                                   capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise RecoveryError('gcloud authentication failed. Use your authorized Cloud Shell account.') from error
        require(bool(token), 'gcloud returned no access token.')
        request = Request(url, method=method,
                          data=None if body is None else canonical(body).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as reply:
                data = reply.read(MAX_RESPONSE_BYTES + 1)
                require(len(data) <= MAX_RESPONSE_BYTES, 'Provider response is too large.')
                return parse_json(data)
        except HTTPError as error:
            raise RecoveryError(f'Google API HTTP {error.code}; provider body suppressed.') from None
        except (URLError, TimeoutError, OSError) as error:
            raise RecoveryError('Google API transport failed; no automatic write retry.') from error


def execute(cloud, target, *, apply=False, expected_revision=None,
            writers_stopped=False, patched_code_verified=False, backup_dir=None, include_subscribers=False):
    """Narrow adapter. Only Firestore's jobs document may be changed."""
    validate_target(target)
    base = ('https://run.googleapis.com/v2/projects/' + target['project']
            + '/locations/' + target['region'] + '/services/' + target['service'])
    service = cloud.request('GET', base)
    revision = serving_revision(service)
    if apply:
        require(patched_code_verified, 'Verify the collection fixes and, for subscriber refresh, the multi-day mySubscribers code are deployed.')
        verify_disabled(service, expected_revision, writers_stopped)
    live_revision = cloud.request('GET', base + '/revisions/' + revision)
    containers = live_revision.get('containers', [])
    require(len(containers) == 1, 'Expected exactly one application container.')
    values = [e.get('value') for e in containers[0].get('env', [])
              if e.get('name') == 'YNA_FIRESTORE_DATABASE']
    require(len(values) == 1 and isinstance(values[0], str)
            and re.fullmatch(r'projects/' + re.escape(target['project']) + r'/databases/([A-Za-z0-9_-]+|\(default\))', values[0]),
            'Deployed Firestore database is absent, indirect or in a different project.')
    root = 'https://firestore.googleapis.com/v1/' + values[0] + '/documents'
    transaction = None
    attempted_commit = False
    try:
        if apply:
            transaction = cloud.request('POST', root + ':beginTransaction',
                                        {'options': {'readWrite': {}}}).get('transaction')
            require(isinstance(transaction, str) and bool(transaction), 'No transaction returned.')
        suffix = '?' + urlencode({'transaction': transaction}) if transaction else ''
        heads = {name: cloud.request('GET', root + '/state/' + name + suffix)
                 for name in ('workspace_access', 'channel_connections', 'collection_jobs')}
        jobs = read_snapshot(heads['collection_jobs'])
        changed, report = prepare_recovery(jobs, read_snapshot(heads['channel_connections']),
                                           read_snapshot(heads['workspace_access']), target, datetime.now(UTC),
                                           include_subscribers=include_subscribers)
        report.update(serving_revision=revision, database=values[0], mode='APPLY' if apply else 'PLAN')
        if not apply or changed is None:
            return report
        name = values[0] + '/documents/state/collection_jobs'
        head = heads['collection_jobs']
        require(head.get('name') == name and isinstance(head.get('updateTime'), str),
                'Jobs document identity/version is missing.')
        metadata = {'report': report, 'target': target,
                    'before_sha256': hashlib.sha256(canonical(jobs).encode()).hexdigest(),
                    'after_sha256': hashlib.sha256(canonical(changed).encode()).hexdigest()}
        path = write_backup(Path(backup_dir or '~/.yna-recovery-backups'), head, metadata)
        # The database transaction protects the three documents; this second
        # service read also rejects an operator re-enabling traffic mid-repair.
        verify_disabled(cloud.request('GET', base), expected_revision, writers_stopped)
        body = {'transaction': transaction, 'writes': [{
            'update': {'name': name, 'fields': {'document': {'stringValue': canonical(changed)}}},
            'updateMask': {'fieldPaths': ['document']},
            'currentDocument': {'updateTime': head['updateTime']}}]}
        attempted_commit = True
        try:
            cloud.request('POST', root + ':commit', body)
            observed = read_snapshot(cloud.request('GET', root + '/state/collection_jobs'))
            require(observed == changed, 'Post-commit verification did not match.')
        except RecoveryError as error:
            raise RecoveryError('COMMIT_OUTCOME_UNCONFIRMED. Do not restore blindly. '
                                'Run PLAN again to inspect the deterministic replacement. '
                                'Backup: ' + str(path)) from error
        report.update(outcome='QUEUED', backup=str(path), verified=True)
        return report
    finally:
        if transaction and not attempted_commit:
            try:
                cloud.request('POST', root + ':rollback', {'transaction': transaction})
            except RecoveryError:
                pass  # Abandoning a read-only attempt cannot mutate application state.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True, help='JSON with the eight target identifiers')
    parser.add_argument('--include-subscribers', action='store_true',
                        help='Also queue one fresh subscriber traversal; old accepted data is preserved')
    parser.add_argument('--apply', action='store_true', help='Queue a replacement (default is read-only PLAN)')
    parser.add_argument('--expected-revision', help='Revision whose patched code you verified')
    parser.add_argument('--writers-stopped', action='store_true', help='Attest all writers/requests have drained')
    parser.add_argument('--patched-code-verified', action='store_true', help='Attest the selected recovery and multi-day collection fixes are deployed')
    parser.add_argument('--backup-dir', type=Path, default=Path.home()/'.yna-recovery-backups')
    args = parser.parse_args(argv)
    try:
        target = parse_json(args.target.read_text(encoding='utf-8'))
        report = execute(Cloud(), target, apply=args.apply, expected_revision=args.expected_revision,
                         writers_stopped=args.writers_stopped, patched_code_verified=args.patched_code_verified,
                         backup_dir=args.backup_dir, include_subscribers=args.include_subscribers)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (RecoveryError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        # Avoid untrusted value reprs in malformed data errors.
        print(str(error) if isinstance(error, RecoveryError) else 'Invalid input or local I/O error; inspect locally.',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
