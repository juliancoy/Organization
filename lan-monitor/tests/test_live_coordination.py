import collections
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from agent_client import wait_for_events
from granular_worker import Dependencies
from snapshot_staging import snapshot, SnapshotStaging
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from launcher_agent import Agent


class LiveCoordinationTests(unittest.TestCase):
    def test_resume_uses_same_cursor_and_never_retries_mutations(self):
        client=Mock()
        client.call.side_effect=[ConnectionResetError(),{'cursor':8,'events':[]}]
        with patch('agent_client.time.sleep'):
            self.assertEqual(wait_for_events(client,'run',7)['cursor'],8)
        self.assertTrue(all(call.args==('job.poll',{'id':'run','cursor':7,'wait_seconds':20}) for call in client.call.call_args_list))

    def test_event_notification_releases_waiting_request(self):
        with tempfile.TemporaryDirectory() as folder:
            agent=Agent.__new__(Agent)
            agent.root=Path(folder);agent.lock=threading.RLock();agent.changed=threading.Condition(agent.lock)
            ident='20261024T000000Z-1234abcd'
            job={'status':'running','sequence':0,'events':collections.deque()};agent.jobs={ident:job}
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(agent.dispatch,'job.poll',{'id':ident,'cursor':0,'wait_seconds':20})
                with agent.changed:
                    job['sequence']=1;job['events'].append((1,{'kind':'capacity','cpus':4}))
                    agent.changed.notify_all()
                result=future.result(timeout=1)
            self.assertEqual(result['cursor'],1)
            self.assertEqual(result['events'][0]['event']['kind'],'capacity')

    def test_prepared_dependencies_require_matching_source_and_images(self):
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder)
            prepared={'source_sha256':'source-a','images':{'node':'image-a'},'packages':['web']}
            (work/'.prepared-packages.json').write_text(json.dumps(prepared))
            manifest={'source_sha256':'source-a','hosts':[{'images':{'node':'image-a'}}]}
            self.assertEqual(Dependencies(work,manifest,{}).results,{'web':0})
            manifest['source_sha256']='source-b'
            self.assertEqual(Dependencies(work,manifest,{}).results,{})
            manifest['source_sha256']='source-a';manifest['hosts'][0]['images']['node']='image-b'
            self.assertEqual(Dependencies(work,manifest,{}).results,{})

    def test_watcher_construction_does_not_start_background_work(self):
        staging=SnapshotStaging('/unused')
        self.assertFalse(staging.thread.is_alive())

    def test_snapshot_discovery_only_reads_commits(self):
        calls=[]
        def git(command,**kwargs):
            calls.append(command)
            operation=command[3]
            return (('a'*40+'\n') if operation=='rev-parse' else 'https://github.com/example/repo.git\n' if operation=='remote' else '').encode()
        with patch('snapshot_staging.subprocess.check_output',side_effect=git):
            refs=snapshot('/checkout')
        self.assertEqual(set(refs),{'.','OrgPortal','PIdP','CodeCollective'})
        self.assertTrue(all(c[3] in ('rev-parse','remote','ls-tree') for c in calls))

    def test_matching_prepared_checkout_is_consumed_without_touching_an_active_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            agent=Agent.__new__(Agent)
            agent.root=root;agent.lock=threading.RLock();agent.changed=threading.Condition(agent.lock)
            agent.config=root/'peers.json';agent.config.write_text(json.dumps({'name':'worker'}))
            old='20261024T000000Z-1234abcd';new='20261024T000001Z-1234abcd'
            source=agent.folder(old)/'source';source.mkdir(parents=True)
            for repo in ('.','OrgPortal','PIdP','CodeCollective'):(source/repo/'.git').mkdir(parents=True)
            (source/'pinned.txt').write_text('immutable source')
            marker={'source_sha256':'digest','images':{'python':'sha256:abc'},'packages':['web']}
            (source/'.prepared-packages.json').write_text(json.dumps(marker))
            work=agent.folder(new)/'source';work.parent.mkdir()
            active=root/'unrelated-active';active.mkdir();(active/'source.txt').write_text('running version')
            agent.jobs={old:{'prepare_only':True,'status':'completed'}}
            job={'sequence':0,'events':collections.deque()}
            manifest={'id':new,'source_sha256':'digest','hosts':[{'host':'worker','images':marker['images']}],
                      'repositories':{'.':{'commit':'commit'}}}
            agent.checkout(work,manifest,{},job)
            self.assertEqual((work/'pinned.txt').read_text(),'immutable source')
            self.assertFalse(source.exists())
            self.assertEqual(agent.jobs[old]['status'],'consumed')
            self.assertEqual((active/'source.txt').read_text(),'running version')
            self.assertEqual(json.loads((work/'distributed-plan.json').read_text())['id'],new)
