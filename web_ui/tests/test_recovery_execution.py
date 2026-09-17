"""The replacement must run through the real orchestration without recollecting subscribers."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import unittest

from channel_connections.models import SubscriberRow
from channel_connections.tests.support import FixedClock
from channel_data.models import CollectionKind, StartCollection
from collection_jobs import snapshot
from collection_jobs.models import EnqueueRun, ExecuteRun, RunKind, RunStatus
from collection_jobs.service import CollectionJobsService
from collection_jobs.tests.support import build_stack, context
from web_ui.recover_owner_content import prepare_recovery


class RecoveryExecutionTest(unittest.TestCase):
    def test_replacement_executes_and_preserves_one_thousand_saved_subscribers(self):
        now = datetime(2026, 9, 17, tzinfo=UTC)
        clock = FixedClock(now)
        stack = build_stack(clock=clock, page_size=50)
        owner = context()
        connection = stack.connect(owner)
        stack.data_gateway.subscribers = tuple(SubscriberRow(
            subscriber_channel_id=f'UC_fixture_{i}', title=f'Fixture {i}',
            api_published_at=now-timedelta(days=2)) for i in range(1000))
        sub_run = stack.jobs.enqueue_run(owner, EnqueueRun(
            connection_id=connection.connection_id, kind=RunKind.SUBSCRIBERS,
            idempotency_key='collect-subs'))
        self.assertIs(stack.jobs.execute_run(owner, ExecuteRun(
            run_id=sub_run.run_id, idempotency_key='execute-subs')).status, RunStatus.SUCCEEDED)
        source = stack.jobs.enqueue_run(owner, EnqueueRun(
            connection_id=connection.connection_id, kind=RunKind.OWNER_CONTENT,
            idempotency_key='orphan-content'))
        old_time = now - timedelta(days=13)
        stack.channel_data.start_collection(owner, StartCollection(
            channel_id=connection.provider_channel_id, collection_id=source.run_id+'-a1-videos',
            kind=CollectionKind.VIDEOS, started_at=old_time, idempotency_key='old-start'))
        original = snapshot.dump(stack.jobs._state)
        jobs = json.loads(original)
        src = next(v['f'] for k,v in jobs['state']['f']['runs']['v'] if v['f']['run_id']==source.run_id)
        src.update(status={'#':'enum','t':'RunStatus','v':'RUNNING'},
                   started_at={'#':'time','v':old_time.isoformat()},
                   enqueued_at={'#':'time','v':old_time.isoformat()})
        target = dict(project='test-project',region='asia-northeast1',service='test-web',
                      workspace_id=owner.workspace_id,channel_id=connection.provider_channel_id,
                      connection_id=connection.connection_id,source_run_id=source.run_id,
                      owner_user_id=owner.user_id)
        cx = {'version':1,'state':{'#':'obj','t':'MemoryState','f':{'connections':{'#':'map','v':[
            [owner.workspace_id+'\x1f'+connection.connection_id,{'#':'obj','t':'ChannelConnection','f':{
                'workspace_id':owner.workspace_id,'connection_id':connection.connection_id,
                'provider_channel_id':connection.provider_channel_id,
                'provider':{'#':'enum','t':'ConnectionProvider','v':'YOUTUBE'},
                'status':{'#':'enum','t':'ConnectionStatus','v':'ACTIVE'}}}]]}}}}
        access = dict(version=1,users=[dict(user_id=owner.user_id,enabled=True,deleted_at=None)],
                      workspaces=[dict(workspace_id=owner.workspace_id)],
                      memberships=[dict(workspace_id=owner.workspace_id,user_id=owner.user_id,role='OWNER')])
        subscriber_fields = ('subscriber_snapshots','subscriber_observations',
                             'subscriber_registry','accepted_subscriber_snapshot')
        before = {name:deepcopy(getattr(stack.channel_data._state,name)) for name in subscriber_fields}
        changed, report = prepare_recovery(jobs,cx,access,target,now)

        class Store:
            def __init__(self, document): self.document=document
            def load(self): return self.document
            def save(self, document): self.document=document

        restored = CollectionJobsService(clock=clock,tokens=stack.jobs._tokens,
            broker=stack.connections,targets=stack.connections,channel_data=stack.channel_data,
            page_size=50,state_store=Store(json.dumps(changed)))
        restored.execute_due_runs(owner,now,20)
        self.assertIs(restored.get_run(owner,report['replacement_run_id']).status,RunStatus.SUCCEEDED)
        for name, value in before.items():
            self.assertEqual(getattr(stack.channel_data._state,name),value,name)
        dataset = stack.channel_data.load_silent_analysis_dataset(owner,connection.provider_channel_id)
        self.assertIsNotNone(dataset.inventory_id)
        self.assertEqual(len(stack.channel_data._state.subscriber_registry),1000)


if __name__ == '__main__': unittest.main()
