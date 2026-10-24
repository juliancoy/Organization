import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from distributed_worker import PYTHON, SUITES
from granular_dispatch import execute


class DispatchLifecycleTests(unittest.TestCase):
    def test_completed_session_drains_events_without_feeding_it_again(self):
        task='monitor--abc'
        events=[]
        state={'status':'running','closes':0}
        host={'host':'worker','session':{'machine':'machine'},'agent_status':{'source_revision':'source'}}
        def call(operation,args=None,**kwargs):
            if operation=='status':return {'ready':True,'machine':'machine','source_revision':'source'}
            if operation=='job.start':
                events.append({'kind':'capacity','cpus':4,'memory_mb':4096})
                return {'status':'running'}
            if operation=='job.poll':
                cursor=args['cursor']
                return {'status':state['status'],'cursor':len(events),
                        'events':[{'sequence':i+1,'event':event} for i,event in enumerate(events) if i+1>cursor]}
            if operation=='job.feed':
                if state['status']!='running':raise RuntimeError('Already completed')
                for ident in args['tasks']:
                    events.append({'kind':'suite','suite':ident,'status':'passed','duration_seconds':1})
                if args.get('close'):
                    state['closes']+=1
                    events.append({'kind':'complete'})
                    state['status']='completed'
                return {'accepted':args['tasks']}
            raise AssertionError(operation)
        with tempfile.TemporaryDirectory() as folder:
            run={'id':'20261024T000000Z-1234abcd','hosts':[host],'images':{},'source_sha256':'digest','force':True,
                 'selection':{'paths':[],'suites':['monitor']},
                 'repositories':{name:{'url':'url','commit':'commit'} for name in ('.','OrgPortal','PIdP')}}
            service=SimpleNamespace(run=run,root=folder,folder=folder,lock=threading.RLock(),save=Mock())
            def update(worker,event):
                if event.get('suite') in worker['jobs']:worker['jobs'][event['suite']].update(status=event['status'])
            service.update=update
            images=Mock(host_images={'worker':{PYTHON:'image'}},images={PYTHON:'image'})
            spec={**SUITES['monitor'],'base':'monitor','cpus':1,'memory_mb':512}
            with patch.dict(SUITES,{task:spec}), patch('granular_dispatch.catalog',return_value=[task]), \
                 patch('granular_dispatch.pending',return_value=[task]), patch('granular_dispatch.SuiteCache'), \
                 patch('granular_dispatch.AgentClient') as client, patch('granular_dispatch.time.sleep'):
                client.return_value.call.side_effect=call
                unused={'host':'unused-worker'}
                execute(service,[host,unused],images)
                self.assertTrue(unused['not_executed'])
                self.assertEqual(client.call_count,1)
            self.assertEqual(host['status'],'passed')
            self.assertEqual(host['jobs'][task]['status'],'passed')
            self.assertEqual(state['closes'],1)
            self.assertEqual(run['pending_tasks'],[])
