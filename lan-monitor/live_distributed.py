"""Load mounted coordinator code for each new run without restarting the API."""
import importlib
import sys
import threading
import copy
import time


class LiveDistributedTests:
    def __init__(self):
        self.lock = threading.RLock()
        self.runner = self.load()
        self.reruns={}
        from test_requests import TestRequests
        self.requests=TestRequests(self)
        from snapshot_staging import SnapshotStaging
        self.staging=SnapshotStaging(self.runner.root)

    @staticmethod
    def load():
        importlib.invalidate_caches()
        for name in ('distributed','run_activity','post_deployment','distributed_worker','suite_cache','work_queue','granular_dispatch','granular_worker','github_sources','distributed_images','visual_artifacts','image_registry','agent_client','agent_updates','p2p_network','docker_utils','docker_utils.images','docker_utils.policy','docker_utils.sdk'):
            sys.modules.pop(name,None)
        return importlib.import_module('distributed').DistributedTests()

    def view(self, ident=None):
        with self.lock:
            selected=self.reruns.get(ident,self.runner)
            result=selected.view(ident)
            result['snapshot_staging']=self.staging.view()
            result['active_reruns']=[r.run['id'] for r in self.reruns.values() if r.running]
            return result

    def artifact(self, run, host, suite, name):
        with self.lock:
            return self.runner.artifact(run, host, suite, name)

    def start(self,force=False):
        with self.lock:
            if self.runner.running or any(r.running for r in self.reruns.values()):
                return False
            self.runner = self.load()
            return self.runner.start(force=force)

    def stop(self):
        with self.lock:
            return self.runner.stop()

    def rerun(self,parent_run_id,host=None,suite=None,failed_only=False):
        with self.lock:
            if sum(r.running for r in self.reruns.values())>=10:raise ValueError('At most ten reruns may be queued')
            parent=self.view(parent_run_id)['run']
            selected=[]
            for original in parent['hosts']:
                if host and original['host']!=host:continue
                jobs={key:job for key,job in original['jobs'].items() if (not suite or key==suite)
                      and (not failed_only or key!='p2p') and (not failed_only or job['status'] in ('failed','timed_out','unavailable','interrupted'))}
                if not jobs:continue
                if 'p2p' in jobs:raise ValueError('P2P requires a full distributed run; select an ordinary suite')
                worker={key:copy.deepcopy(original[key]) for key in ('host','local') if key in original}
                worker.update(status='queued',suites=list(jobs),jobs={key:{'name':job['name'],'status':'queued','phase':None,'logs':[]} for key,job in jobs.items()})
                selected.append(worker)
            if not selected:raise ValueError('No matching suites to rerun')
            from distributed import DistributedTests
            runner=DistributedTests(self.runner.root,self.runner.folder,recover=False)
            runner.publication_lock=self.runner.publication_lock
            runner.start(selected,parent_run_id,launch=False)
            self.reruns[runner.run['id']]=runner
            def dispatch():
                while True:
                    with self.lock:
                        if runner.run.get('stop_requested'):
                            runner.run.update(status='stopped',finished=__import__('distributed').now());runner.running=False;runner.save();return
                        required={h['host'] for h in selected}
                        active=[r for r in [self.runner,*self.reruns.values()] if r is not runner and r.running and r.run.get('status')!='queued']
                        if not any(required.intersection(h['host'] for h in r.run['hosts']) for r in active):
                            runner.run['status']='preparing';runner.save();break
                    time.sleep(1)
                runner.execute()
            threading.Thread(target=dispatch,daemon=True).start()
            return runner.run['id']

    def stop_run(self,ident):
        with self.lock:
            runner=self.reruns.get(ident)
            if runner is None:
                if self.runner.run.get('id')!=ident:return False
                runner=self.runner
            if runner.run.get('status')=='queued':
                runner.run['stop_requested']=True;runner.save();return True
            return runner.stop()
