"""Explicit subscriber refresh is additive and never rewrites accepted data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from web_ui import recover_owner_content as recovery
from web_ui.tests.test_owner_content_recovery import TARGET, NOW, FakeCloud, fixtures, enum, moment


class SubscriberRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.jobs, self.connections, self.access = fixtures()

    def prepare(self, jobs=None):
        return recovery.prepare_recovery(self.jobs if jobs is None else jobs,
            self.connections, self.access, TARGET, NOW, include_subscribers=True)

    def test_opt_in_adds_two_distinct_jobs_and_preserves_old_success_and_all_other_state(self):
        original = deepcopy((self.jobs, self.connections, self.access))
        updated, report = self.prepare()
        self.assertEqual((self.jobs, self.connections, self.access), original)
        f, before = updated['state']['f'], self.jobs['state']['f']
        self.assertEqual(f['runs']['v'][1], before['runs']['v'][1])
        self.assertEqual(len(f['runs']['v']), len(before['runs']['v']) + 2)
        for field in before.keys() - {'runs', 'revisions'}:
            self.assertEqual(f[field], before[field])
        from collection_jobs.snapshot import load
        from collection_jobs.models import RunKind, RunStatus
        decoded = load(json.dumps(updated))
        fresh = decoded.runs[TARGET['workspace_id']+'\x1f'+report['subscriber_run_id']]
        self.assertEqual(fresh.kind, RunKind.SUBSCRIBERS)
        self.assertEqual(fresh.status, RunStatus.QUEUED)
        self.assertEqual(fresh.quota_spent, 0)
        self.assertEqual(fresh.pages_fetched, 0)
        self.assertTrue(report['subscriber_recollection'])

    def test_default_stays_content_only(self):
        updated, report = recovery.prepare_recovery(self.jobs, self.connections, self.access, TARGET, NOW)
        self.assertEqual(len(updated['state']['f']['runs']['v']), 3)
        self.assertFalse(report['subscriber_recollection'])

    def test_enable_after_prior_content_recovery_only_adds_subscriber_job(self):
        content, _ = recovery.prepare_recovery(self.jobs, self.connections, self.access, TARGET, NOW)
        updated, _ = self.prepare(content)
        self.assertEqual(updated['state']['f']['runs']['v'][:-1], content['state']['f']['runs']['v'])
        self.assertEqual(len(updated['state']['f']['runs']['v']), 4)

    def test_repeated_opt_in_never_creates_more_jobs_even_after_completion(self):
        updated, first = self.prepare()
        for status in ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED'):
            with self.subTest(status=status):
                updated['state']['f']['runs']['v'][-1][1]['f']['status']['v'] = status
                changed, report = self.prepare(updated)
                self.assertIsNone(changed)
                self.assertEqual(report['outcome'], 'ALREADY_PREPARED')
                self.assertEqual(report['subscriber_run_id'], first['subscriber_run_id'])
                self.assertEqual(report['subscriber_status'], status)

    def test_active_or_newer_subscriber_work_refuses_without_partial_mutation(self):
        for status in ('QUEUED', 'RUNNING', 'SUCCEEDED'):
            with self.subTest(status=status):
                self.jobs, self.connections, self.access = fixtures()
                record = self.jobs['state']['f']['runs']['v'][1][1]['f']
                record.update(status=enum('RunStatus', status), enqueued_at=moment(NOW.isoformat()))
                before = deepcopy(self.jobs)
                with self.assertRaises(recovery.RecoveryError):
                    self.prepare()
                self.assertEqual(self.jobs, before)

    def test_opt_in_does_not_bypass_owner_or_connection_validation(self):
        self.access['memberships'] = []
        with self.assertRaises(recovery.RecoveryError):
            self.prepare()

    def test_plan_reads_only_and_apply_commits_both_jobs_in_one_document(self):
        cloud = FakeCloud(recovery)
        plan = recovery.execute(cloud, TARGET, include_subscribers=True)
        self.assertTrue(plan['subscriber_recollection'])
        self.assertTrue(all(m == 'GET' for m, _, _ in cloud.calls))
        with tempfile.TemporaryDirectory() as directory:
            applied = recovery.execute(cloud, TARGET, apply=True, include_subscribers=True,
                expected_revision='test-web-00022', writers_stopped=True,
                patched_code_verified=True, backup_dir=Path(directory))
        self.assertTrue(applied['verified'])
        self.assertEqual(len(cloud.commit_body['writes']), 1)
        document = cloud.commit_body['writes'][0]['update']['fields']['document']['stringValue']
        self.assertEqual(len(json.loads(document)['state']['f']['runs']['v']), 4)
