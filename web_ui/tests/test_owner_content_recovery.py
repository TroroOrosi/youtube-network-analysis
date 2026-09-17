"""Synthetic administrative recovery tests; no real users or credentials."""
from copy import deepcopy
from datetime import datetime, UTC, timedelta
from importlib import util
import json
import os
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).parents[1] / 'recover_owner_content.py'
NOW = datetime(2026, 9, 17, tzinfo=UTC)
START = (NOW - timedelta(days=13)).isoformat()
TARGET = dict(project='test-project', region='asia-northeast1', service='test-web',
              workspace_id='workspace-test', channel_id='UC-test',
              connection_id='connection-test', source_run_id='run-old',
              owner_user_id='user-owner')

def enum(t, v): return {'#': 'enum', 't': t, 'v': v}
def obj(t, **f): return {'#': 'obj', 't': t, 'f': f}
def mapping(pairs=()): return {'#': 'map', 'v': list(pairs)}
def moment(s): return {'#': 'time', 'v': s}

def fixtures():
    run = obj('CollectionRun', run_id='run-old', workspace_id='workspace-test',
              connection_id='connection-test', provider_channel_id='UC-test',
              kind=enum('RunKind', 'OWNER_CONTENT'), status=enum('RunStatus', 'RUNNING'),
              attempt=1, enqueued_at=moment(START), started_at=moment(START),
              finished_at=None, pages_fetched=0, quota_spent=0,
              failure_reason=None, next_attempt_at=None)
    sub = deepcopy(run)
    sub['f'].update(run_id='run-sub', kind=enum('RunKind', 'SUBSCRIBERS'),
                    status=enum('RunStatus', 'SUCCEEDED'), finished_at=moment(START),
                    pages_fetched=20, quota_spent=20)
    jobs = {'version': 1, 'state': obj('MemoryState',
        runs=mapping([['workspace-test\x1frun-old', run], ['workspace-test\x1frun-sub', sub]]),
        quota=mapping([[{'#':'tuple','v':['workspace-test','2026-09-04']},
                        obj('QuotaLedgerEntry', used_units=39, first_used_at=moment(START))]]),
        schedules=mapping(), idempotency=mapping(), cursors=mapping(),
        revisions=mapping([['workspace-test', 5], ['workspace-other', 8]]), resume=mapping())}
    connection = obj('ChannelConnection', connection_id='connection-test',
                     workspace_id='workspace-test', provider_channel_id='UC-test',
                     provider=enum('ConnectionProvider', 'YOUTUBE'),
                     status=enum('ConnectionStatus','ACTIVE'))
    connections = {'version':1,'state':obj('MemoryState', connections=mapping([
        ['workspace-test\x1fconnection-test', connection]]))}
    access = {'version':1,'users':[{'user_id':'user-owner','enabled':True,'deleted_at':None}],
              'workspaces':[{'workspace_id':'workspace-test'}],
              'memberships':[{'workspace_id':'workspace-test','user_id':'user-owner','role':'OWNER'}],
              'sessions':[{'secret_digest':'private-test-only'}]}
    return jobs, connections, access

class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if MODULE.exists():
            spec = util.spec_from_file_location('recovery_under_test', MODULE)
            cls.mod = util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        else:
            cls.mod = None

    def setUp(self):
        self.assertIsNotNone(self.mod, 'Targeted recovery CLI has not been implemented')
        self.jobs, self.connections, self.access = fixtures()

    def prepare(self, **kwargs):
        return self.mod.prepare_recovery(self.jobs, self.connections, self.access,
                                         TARGET, NOW, **kwargs)

    def test_only_old_run_new_run_and_workspace_revision_change(self):
        original = deepcopy((self.jobs, self.connections, self.access))
        updated, report = self.prepare()
        self.assertEqual((self.jobs,self.connections,self.access), original)
        fields, old = updated['state']['f'], original[0]['state']['f']
        self.assertEqual(fields['runs']['v'][1], old['runs']['v'][1])
        for name in old.keys() - {'runs','revisions'}:
            self.assertEqual(fields[name], old[name], name)
        self.assertEqual(fields['revisions']['v'], [['workspace-test',6],['workspace-other',8]])
        self.assertEqual(fields['runs']['v'][0][1]['f']['status']['v'], 'FAILED')
        self.assertEqual(fields['runs']['v'][0][1]['f']['quota_spent'], 0)
        replacement = fields['runs']['v'][-1][1]['f']
        self.assertEqual(replacement['status']['v'], 'QUEUED')
        self.assertEqual(replacement['kind']['v'], 'OWNER_CONTENT')
        self.assertEqual(replacement['workspace_id'], TARGET['workspace_id'])
        self.assertNotEqual(replacement['run_id'], TARGET['source_run_id'])
        self.assertEqual(report['outcome'], 'READY_TO_APPLY')
        self.assertNotIn('private-test-only', json.dumps(report))

    def test_repeat_does_not_enqueue_again_even_after_replacement_finished(self):
        updated, report = self.prepare()
        for status in ('QUEUED','RUNNING','SUCCEEDED','FAILED'):
            with self.subTest(status=status):
                updated['state']['f']['runs']['v'][-1][1]['f']['status']['v'] = status
                changed, replay = self.mod.prepare_recovery(updated, self.connections, self.access, TARGET, NOW)
                self.assertIsNone(changed)
                self.assertEqual(replay['outcome'], 'ALREADY_PREPARED')
                self.assertEqual(replay['replacement_status'], status)

    def test_startup_failed_record_can_be_recovered_without_overwriting_evidence(self):
        src = self.jobs['state']['f']['runs']['v'][0][1]['f']
        src.update(status=enum('RunStatus','FAILED'),
                   failure_reason=enum('RunFailureReason','UNEXPECTED_FAILURE'),
                   finished_at=moment((NOW-timedelta(minutes=1)).isoformat()))
        changed, _ = self.prepare()
        self.assertEqual(changed['state']['f']['runs']['v'][0], self.jobs['state']['f']['runs']['v'][0])

    def test_refuse_non_owner_disabled_owner_wrong_channel_or_connection(self):
        for edit in ('membership','disabled','channel','connection','workspace','reauth'):
            with self.subTest(edit=edit):
                self.jobs,self.connections,self.access=fixtures()
                fields=self.connections['state']['f']['connections']['v'][0][1]['f']
                if edit=='membership': self.access['memberships']=[]
                if edit=='disabled': self.access['users'][0]['enabled']=False
                if edit=='channel': fields['provider_channel_id']='UC-other'
                if edit=='connection': fields['connection_id']='connection-other'
                if edit=='workspace': fields['workspace_id']='workspace-other'
                if edit=='reauth': fields['status']['v']='REAUTH_REQUIRED'
                with self.assertRaises(self.mod.RecoveryError): self.prepare()

    def test_refuse_recent_or_resumable_run_or_newer_content_run(self):
        for edit in ('recent','checkpoint','resume','newer','succeeded','cancelled','version'):
            with self.subTest(edit=edit):
                self.jobs,self.connections,self.access=fixtures()
                fields=self.jobs['state']['f']; src=fields['runs']['v'][0][1]['f']
                if edit=='recent': src['started_at']=moment(NOW.isoformat())
                if edit=='checkpoint': fields['page_checkpoints']=mapping([['workspace-test\x1frun-old',{}]])
                if edit=='resume': fields['resume']=mapping([['workspace-test\x1frun-old',{}]])
                if edit=='newer':
                    other=deepcopy(fields['runs']['v'][0]);other[0]='workspace-test\x1frun-newer'
                    other[1]['f'].update(run_id='run-newer',enqueued_at=moment(NOW.isoformat()))
                    fields['runs']['v'].append(other)
                if edit in ('succeeded','cancelled'): src['status']['v']=edit.upper()
                if edit=='version': self.jobs['version']=99
                with self.assertRaises(self.mod.RecoveryError): self.prepare()

    def test_refuse_duplicate_map_keys_and_mismatched_record_id(self):
        self.jobs['state']['f']['runs']['v'].append(deepcopy(self.jobs['state']['f']['runs']['v'][0]))
        with self.assertRaises(self.mod.RecoveryError): self.prepare()
        self.jobs,self.connections,self.access=fixtures()
        self.jobs['state']['f']['runs']['v'][0][1]['f']['run_id']='different'
        with self.assertRaises(self.mod.RecoveryError): self.prepare()

    def test_domain_codec_accepts_repaired_v1_snapshot(self):
        from collection_jobs.snapshot import load
        from collection_jobs.models import RunKind, RunStatus
        updated, report=self.prepare(); state=load(json.dumps(updated))
        new=state.runs['workspace-test\x1f'+report['replacement_run_id']]
        self.assertIs(new.kind,RunKind.OWNER_CONTENT); self.assertIs(new.status,RunStatus.QUEUED)
        self.assertEqual(state.quota[('workspace-test','2026-09-04')].used_units,39)
        self.assertIs(state.runs['workspace-test\x1frun-sub'].status,RunStatus.SUCCEEDED)

    def test_single_document_guard(self):
        head={'fields':{'parts':{'integerValue':'1'},'document':{'stringValue':json.dumps(self.jobs)}}}
        self.assertEqual(self.mod.read_snapshot(head), self.jobs)
        for bad in ({'generation':{'stringValue':'a'*32}}, {'parts':{'integerValue':'2'}},
                    {'document':{'stringValue':'broken'}}):
            broken=deepcopy(head);broken['fields'].update(bad)
            with self.assertRaises(self.mod.RecoveryError): self.mod.read_snapshot(broken)

    def test_disabled_service_guard_refuses_tags_reconciliation_and_wrong_revision(self):
        service={'scaling':{'scalingMode':'MANUAL','manualInstanceCount':0},
                 'traffic':[{'revision':'test-web-00022','percent':100}],
                 'trafficStatuses':[{'revision':'test-web-00022','percent':100}],
                 'generation':'2','observedGeneration':'2','reconciling':False}
        self.mod.verify_disabled(service,'test-web-00022',True)
        for edit in ('auto','tag','reconciling','revision','unattested'):
            with self.subTest(edit=edit):
                bad=deepcopy(service)
                if edit=='auto': bad['scaling']['scalingMode']='AUTOMATIC'
                if edit=='tag': bad['traffic'].append({'revision':'older','tag':'preview'})
                if edit=='reconciling': bad['reconciling']=True
                rev='wrong' if edit=='revision' else 'test-web-00022'
                with self.assertRaises(self.mod.RecoveryError):
                    self.mod.verify_disabled(bad,rev,edit!='unattested')

    @unittest.skipUnless(os.name == "posix", "Cloud Shell owner-only permissions")
    def test_backup_created_exclusively_owner_only_without_access_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            head={'fields':{'document':{'stringValue':json.dumps(self.jobs)}},'updateTime':'time'}
            path=self.mod.write_backup(Path(d)/'backups',head,{'operation':'test'})
            content=json.loads(path.read_text())
            self.assertEqual(content['jobs_document'],head)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertNotIn('private-test-only',path.read_text())



class FakeCloud:
    def __init__(self, mod):
        self.mod=mod; self.calls=[]; self.commit_body=None; self.commit_error=False
        self.race=False; self.verify_error=False
        jobs,connections,access=fixtures()
        self.service={'scaling':{'scalingMode':'MANUAL','manualInstanceCount':0},
                      'traffic':[{'revision':'test-web-00022','percent':100}],
                      'trafficStatuses':[{'revision':'test-web-00022','percent':100}],
                      'generation':'2','observedGeneration':'2','reconciling':False}
        self.root='projects/test-project/databases/(default)/documents'
        self.heads={name:{'name':self.root+'/state/'+name,'updateTime':'2026-09-04T00:00:00Z',
                          'fields':{'document':{'stringValue':json.dumps(value)},'parts':{'integerValue':'1'}}}
                    for name,value in [('workspace_access',access),('channel_connections',connections),('collection_jobs',jobs)]}

    def request(self, method, url, body=None):
        self.calls.append((method,url,deepcopy(body)))
        if url.startswith('https://run.googleapis.com/'):
            if '/revisions/' in url:
                return {'containers':[{'env':[{'name':'YNA_FIRESTORE_DATABASE','value':'projects/test-project/databases/(default)'}]}]}
            if self.race and sum(u==url for _,u,_ in self.calls)>1:
                self.service['scaling']['scalingMode']='AUTOMATIC'
            return deepcopy(self.service)
        if url.endswith(':beginTransaction'): return {'transaction':'synthetic-tx'}
        if url.endswith(':rollback'): return {}
        if url.endswith(':commit'):
            self.commit_body=deepcopy(body)
            update=body['writes'][0]['update']
            self.heads['collection_jobs']['fields'].update(update['fields'])
            if self.commit_error: raise self.mod.RecoveryError('lost reply')
            return {'writeResults':[{'updateTime':'2026-09-17T00:00:00Z'}]}
        name=url.split('/state/')[1].split('?')[0]
        if self.verify_error and self.commit_body:
            raise self.mod.RecoveryError('verification read unavailable')
        return deepcopy(self.heads[name])


@unittest.skipUnless(os.name == "posix", "Cloud Shell apply adapter")
class CloudRecoveryTests(unittest.TestCase):
    """Transport double only: exercise the production adapter, not live GCP."""
    @classmethod
    def setUpClass(cls):
        RecoveryTests.setUpClass()
        cls.mod = RecoveryTests.mod

    def execute(self, cloud, directory, **kwargs):
        return self.mod.execute(cloud,TARGET,apply=True,expected_revision='test-web-00022',
                                writers_stopped=True,patched_code_verified=True,backup_dir=directory,**kwargs)

    def test_plan_only_reads_and_never_backs_up_or_commits(self):
        cloud=FakeCloud(self.mod)
        with tempfile.TemporaryDirectory() as d:
            report=self.mod.execute(cloud,TARGET,backup_dir=Path(d))
            self.assertEqual(report['mode'],'PLAN')
            self.assertEqual(list(Path(d).iterdir()),[])
            self.assertTrue(all(m=='GET' for m,_,_ in cloud.calls))

    def test_apply_uses_transaction_and_only_writes_jobs_field(self):
        cloud=FakeCloud(self.mod);original=deepcopy(cloud.heads)
        with tempfile.TemporaryDirectory() as d:
            report=self.execute(cloud,Path(d))
            self.assertEqual(report['outcome'],'QUEUED'); self.assertTrue(report['verified'])
            self.assertTrue(Path(report['backup']).exists())
        body=cloud.commit_body
        self.assertEqual(body['transaction'],'synthetic-tx')
        self.assertEqual(len(body['writes']),1)
        write=body['writes'][0]
        self.assertEqual(write['update']['name'],cloud.root+'/state/collection_jobs')
        self.assertEqual(write['updateMask'],{'fieldPaths':['document']})
        self.assertEqual(write['currentDocument'],{'updateTime':original['collection_jobs']['updateTime']})
        self.assertEqual(cloud.heads['workspace_access'],original['workspace_access'])
        self.assertEqual(cloud.heads['channel_connections'],original['channel_connections'])
        reads=[u for m,u,_ in cloud.calls if m=='GET' and '/state/' in u]
        self.assertTrue(all('transaction=synthetic-tx' in u for u in reads[:3]))
        self.assertFalse(any('channel_data' in u or 'secretmanager' in u or 'youtube' in u for _,u,_ in cloud.calls))

    def test_lost_commit_response_is_not_retried_and_next_plan_detects_existing(self):
        cloud=FakeCloud(self.mod); cloud.commit_error=True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(self.mod.RecoveryError,'COMMIT_OUTCOME_UNCONFIRMED'):
                self.execute(cloud,Path(d))
            self.assertEqual(sum(u.endswith(':commit') for _,u,_ in cloud.calls),1)
            self.assertEqual(len(list(Path(d).glob('*.json'))),1)
            report=self.mod.execute(cloud,TARGET)
            self.assertEqual(report['outcome'],'ALREADY_PREPARED')

    def test_reenabled_service_after_backup_prevents_commit(self):
        cloud=FakeCloud(self.mod);cloud.race=True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(self.mod.RecoveryError): self.execute(cloud,Path(d))
        self.assertIsNone(cloud.commit_body)
        self.assertTrue(any(u.endswith(':rollback') for _,u,_ in cloud.calls))

    def test_unconfirmed_code_or_enabled_service_refuses_before_transaction(self):
        for edited in ('code','enabled'):
            cloud=FakeCloud(self.mod)
            if edited=='enabled': cloud.service['scaling']['scalingMode']='AUTOMATIC'
            with self.assertRaises(self.mod.RecoveryError):
                self.mod.execute(cloud,TARGET,apply=True,expected_revision='test-web-00022',
                                 writers_stopped=True,patched_code_verified=edited!='code')
            self.assertFalse(any(m=='POST' for m,_,_ in cloud.calls))

    def test_repeated_apply_is_noop_and_rolls_back_read_transaction(self):
        cloud=FakeCloud(self.mod)
        with tempfile.TemporaryDirectory() as d:
            self.execute(cloud,Path(d)); count=len(cloud.calls)
            report=self.execute(cloud,Path(d))
            self.assertEqual(report['outcome'],'ALREADY_PREPARED')
            self.assertFalse(any(u.endswith(':commit') for _,u,_ in cloud.calls[count:]))
            self.assertTrue(any(u.endswith(':rollback') for _,u,_ in cloud.calls[count:]))

    def test_backup_failure_prevents_commit(self):
        cloud=FakeCloud(self.mod)
        with tempfile.TemporaryDirectory() as d:
            blocked=Path(d)/'file'; blocked.write_text('not a directory')
            with self.assertRaises(OSError): self.execute(cloud,blocked)
        self.assertIsNone(cloud.commit_body)

    def test_post_commit_read_failure_reports_unconfirmed_not_success(self):
        cloud=FakeCloud(self.mod);cloud.verify_error=True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(self.mod.RecoveryError,'COMMIT_OUTCOME_UNCONFIRMED'):
                self.execute(cloud,Path(d))
        self.assertEqual(sum(u.endswith(':commit') for _,u,_ in cloud.calls),1)


if __name__ == "__main__":
    unittest.main()
