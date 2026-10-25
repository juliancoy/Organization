"""Live host CPU, memory, disk I/O, and network samples without installing agents."""
import concurrent.futures
import json
import subprocess
import threading
import time
import sys
import math
from paths import WORKSPACE
sys.path.insert(0,str(WORKSPACE/'scripts'))
from launcher_agent import hardware_sample, ResourceSampler
from distributed import targets
from agent_client import AgentClient, CONFIG


def aggregate(hosts):
    fresh=[h for h in hosts if isinstance(h.get('percent'),(int,float)) and math.isfinite(h['percent'])
           and 0<=h['percent']<=100 and isinstance(h.get('cpus'),int) and h['cpus']>0 and h.get('status')=='live']
    capacity=sum(h['cpus'] for h in fresh)
    return round(sum(h['percent']*h['cpus'] for h in fresh)/capacity,1) if capacity else None


class CpuMonitor:
    def __init__(self, activity=None):
        self.lock=threading.Lock()
        self.hosts=[]
        self.sampler=ResourceSampler()
        self.activity=activity

    def sample(self, target):
        local=target.get('local',False)
        try:
            if local:
                values=self.sampler.sample()
                hardware=hardware_sample(disk=WORKSPACE)
                try:
                    config=json.loads(CONFIG.read_text())
                    client=AgentClient({'host':config['name'],'agent_endpoint':'http://'+config['peers'][config['name']]+':8790'})
                    try:detail=client.call('status',timeout=3)
                    finally:client.close()
                    hardware={**detail.get('hardware',{}),**hardware}
                except (OSError,ValueError,RuntimeError,KeyError):pass
                values['hardware']=hardware
            else:
                config=json.loads(CONFIG.read_text())
                address=config['peers'][target['host']]
                client=AgentClient({**target,'agent_endpoint':'http://'+address+':8790'})
                try:values=client.call('metrics',timeout=8)
                finally:client.close()
            if values.get('measurement_version')!=2:
                return {'host':target['host'],'percent':None,'cpus':values.get('cpus'),'status':'update_required','sampled_at':time.time()}
            return {'host':target['host'],**values}
        except (OSError,ValueError,TypeError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
            status='permission_denied' if isinstance(error,PermissionError) else 'authentication_denied' if str(error) in ('Agent HTTP status 401','Agent HTTP status 403') else 'unavailable'
            return {'host':target['host'],'percent':None,'cpus':None,'status':status,'sampled_at':time.time()}

    def view(self):
        with self.lock:
            hosts=[dict(h) for h in self.hosts]
        activity={};activity_status='available' if self.activity else 'unavailable'
        if self.activity:
            try:
                primary=self.activity()
                reports=[primary]+[self.activity(ident) for ident in primary.get('active_reruns',[])]
                for report in reports:
                    if not report.get('running'):continue
                    for host in (report.get('run') or {}).get('hosts',[]):
                        jobs=list(host.get('jobs',{}).values())
                        executing=[j for j in jobs if j.get('status')=='running' and j.get('phase') not in ('prepare','waiting_for_dependencies')]
                        entry=activity.setdefault(host['host'],{'running_test_jobs':0,'scheduled_cpu_slots':0,'workload_phase':'idle'})
                        entry['running_test_jobs']+=len(executing)
                        entry['scheduled_cpu_slots']+=sum(j.get('cpus',0) for j in jobs if j.get('status') in ('queued','running'))
                        phase='testing' if executing else 'preparing' if host.get('status')=='preparing' or any(v.get('status')=='preparing' for v in host.get('dependency_preparation',{}).values()) else 'waiting' if host.get('active_tasks') else 'idle'
                        if phase=='testing' or entry['workload_phase']!='testing':entry['workload_phase']=phase
            except (OSError,ValueError,TypeError,KeyError,RuntimeError):activity={};activity_status='unavailable'
        now=time.time()
        for host in hosts:
            host['sample_age_seconds']=round(max(0,now-host['sampled_at']),1)
            host.update(activity.get(host['host'],{'running_test_jobs':0,'scheduled_cpu_slots':0,'workload_phase':'idle' if activity_status=='available' else 'unknown'}))
            if host['sample_age_seconds']>15:
                host.update(status='stale',percent=None,logical_cpus=[],ram_percent=None,disk_busy_percent=None,network_mbps=None)
                if host.get('hardware'):host['hardware']={**host['hardware'],'stale':True}
        live=[h for h in hosts if h['status']=='live' and h.get('percent') is not None]
        active=[h for h in hosts if h['running_test_jobs']>0] if activity_status=='available' else []
        return {'hosts':hosts,'total_percent':aggregate(hosts),'live_hosts':len(live),
                'measured_logical_cpus':sum(h['cpus'] for h in live),
                'active_test_percent':aggregate(active),'test_hosts':len(active),'measured_test_hosts':sum(h['status']=='live' for h in active),
                'activity_status':activity_status,
                'total_hosts':len(hosts),'sample_interval_seconds':5,
                'sampling_method':'Counter deltas across the full interval; first sample and counter resets warm up without reporting zero.',
                'scope':'Whole-machine executing CPU time, weighted by logical CPU count. I/O wait and stolen time are separate. Test-active machines include all their processes, not only tests. Unavailable and stale hosts are excluded.'}

    def worker(self):
        while True:
            start=time.monotonic()
            try:
                inventory=targets()
            except (OSError,ValueError):
                inventory=[]
            hosts=[{'host':'compai.local','local':True}]+[h for h in inventory if h['host']!='compai.local']
            with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
                samples=list(pool.map(self.sample,hosts))
            with self.lock:
                self.hosts=samples
            time.sleep(max(0.1,5-(time.monotonic()-start)))
