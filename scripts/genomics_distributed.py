#!/usr/bin/env python3
"""Explicit private genomic batch run: enrolled workers, SSH streams, RAM-only input."""
import argparse
from collections import Counter
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid

from scripts.genomics import private_run,write_json
from scripts.genomics_compute import compute,MAX_PAYLOAD

ROOT=Path(__file__).resolve().parents[1]
WORKER=(ROOT/'scripts/genomics_compute.py').read_text()


def shell_command(host,command):
    if host['local']:return command
    return host['ssh'][:-1]+['-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',host['ssh'][-1],shlex.join(command)]


def eligible(status):
    return bool(status.get('ready') and status.get('protocol')=='lifetech-agent.v1' and
                status.get('worker_policy',{}).get('accepting_jobs'))


def discover():
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0,str(ROOT/'swarm-control/server'))
    from agent_client import AgentClient,CONFIG
    from docker_utils.policy import inventory,ssh
    from docker_utils.images import ImageResolver
    config=json.loads(CONFIG.read_text());accounts={h['host']:h for h in inventory()}
    resolver=ImageResolver([],lambda h:h['ssh'])
    expected=resolver.fingerprint(None,'python:3.12-bookworm')
    if not expected:raise RuntimeError('Approved local Python image unavailable')
    def check(item):
        name,address=item
        host={'host':name,'local':name==config['name'],'agent_endpoint':'http://'+address+':8790'}
        client=None
        try:
            client=AgentClient(host);status=client.call('status',timeout=15)
            if not eligible(status):raise RuntimeError('Agent is unavailable or declining jobs')
            if address not in status.get('addresses',[]):raise RuntimeError('Agent address attestation failed')
            if not host['local']:
                if name not in accounts:raise RuntimeError('No pinned SSH account')
                # Host alias is pinned to known_hosts; never autoaccept a key or forward an agent.
                host['ssh']=ssh(accounts[name])[:-1]+['-4','-o','HostName='+address,'-o','HostKeyAlias='+name,ssh(accounts[name])[-1]]
            limits=subprocess.check_output(shell_command(host,['docker','info','--format','{{json .MemoryLimit}} {{json .SwapLimit}}']),text=True,stderr=subprocess.DEVNULL,timeout=30).split()
            if limits!=['true','true']:raise RuntimeError('Docker memory/swap isolation unavailable')
            image=subprocess.check_output(shell_command(host,['docker','image','inspect','python:3.12-bookworm','--format','{{.Id}}']),text=True,stderr=subprocess.DEVNULL,timeout=20).strip()
            fingerprint=resolver.fingerprint(None if host['local'] else host,'python:3.12-bookworm')
            if fingerprint!=expected or not image.startswith('sha256:'):raise RuntimeError('Worker does not have the approved Python image contents')
            host['image_fingerprint']=fingerprint
            host['image']=image
            host['eligible']=True;host['machine']=status['machine']
        except Exception as error:host.update(eligible=False,reason=type(error).__name__+': '+str(error)[:240])
        finally:
            if client:client.close()
        return host
    with ThreadPoolExecutor(max_workers=min(8,len(config['peers']))) as pool:return list(pool.map(check,config['peers'].items()))


def batches(path,kind,limit=50000):
    if not 1<=limit<=100000:raise ValueError('Invalid batch size')
    header=[];body=[];chrom=None;number=0
    def package():
        raw=''.join(header+body).encode();payload=gzip.compress(raw,compresslevel=1,mtime=0)
        if len(payload)>MAX_PAYLOAD:raise ValueError('Batch exceeds RAM input bound; reduce --batch-records')
        return {'id':kind+'-'+str(number),'kind':kind,'records':len(body),'payload':payload,'sha256':hashlib.sha256(payload).hexdigest()}
    with gzip.open(path,'rt') if str(path).endswith('.gz') else open(path) as stream:
        for line in stream:
            if line.startswith('#'):
                if body:raise ValueError('Header appeared after records')
                header.append(line);continue
            current=line.split('\t',1)[0]
            if body and (len(body)>=limit or current!=chrom):number+=1;yield package();body=[]
            chrom=current;body.append(line)
        if body:number+=1;yield package()


def execute(host,batch):
    name='genomics-'+uuid.uuid4().hex
    command=shell_command(host,['docker','run','--name',name,'--label','org.lifetech.genomics=true','--rm','--pull=never','-i','--network','none','--read-only',
        '--log-driver','none','--memory','512m','--memory-swap','512m','--cpus','1','--pids-limit','32',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0',
        host['image'],'python','-c',WORKER,batch['kind']])
    started=time.monotonic()
    try:
        result=subprocess.run(command,input=batch['payload'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=600)
        if result.returncode:raise RuntimeError('Worker computation or SSH transport failed')
    except Exception:
        try:subprocess.run(shell_command(host,['docker','rm','-f',name]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        except (OSError,subprocess.SubprocessError):pass
        raise
    if len(result.stdout)>1024*1024:raise RuntimeError('Summary exceeds 1 MiB')
    data=json.loads(result.stdout)
    if data.get('sha256')!=batch['sha256'] or data.get('records')!=batch['records'] or data.get('kind')!=batch['kind']:raise RuntimeError('Worker result does not match its batch')
    return {**data,'host':host['host'],'elapsed_seconds':round(time.monotonic()-started,3),'input_bytes':len(batch['payload'])}


def merge(results,kind):
    chosen=[r for r in results if r['kind']==kind];merged={'kind':kind,'batches':len(chosen)}
    for key in ['records','records_with_csq','depth_sum','depth_count','quality_sum','quality_count','transitions','transversions']:
        merged[key]=sum(r.get(key,0) for r in chosen)
    for key in ['consequences','filters','variant_types','genotypes','depth_bins','quality_bins']:
        count=Counter()
        for row in chosen:count.update(row.get(key,{}))
        merged[key]=dict(count)
    return merged


class Run:
    def __init__(self,hosts,folder,slots=2):
        self.hosts=[h for h in hosts if h['eligible']];self.folder=folder;self.lock=threading.RLock();self.slots=slots
        self.queue=queue.Queue(maxsize=max(2,len(self.hosts)*slots));self.results=[]
        self.state={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'running',
                    'workers':[{k:v for k,v in h.items() if k not in ('ssh','agent_endpoint')} for h in hosts],
                    'jobs':{},'sources':{},'scope':'Private streamed QC and existing-annotation audit; no fresh VEP run. Remote inputs remain in process memory only.'}
        if not self.hosts:raise RuntimeError('No authenticated, accepting workers with pinned SSH access')

    def save(self):
        with self.lock:
            temporary=self.folder/'state.tmp';temporary.write_text(json.dumps(self.state,indent=2));temporary.chmod(0o600);temporary.replace(self.folder/'state.json')

    def worker(self,host):
        while True:
            item=self.queue.get()
            try:
                if item is None:return
                with self.lock:self.state['jobs'][item['id']].update(status='running',host=host['host']);self.save()
                try:
                    # Power/deny changes are respected between batches, not just at discovery.
                    from agent_client import AgentClient
                    client=AgentClient(host)
                    try:
                        deadline=time.monotonic()+1800
                        while True:
                            status=client.call('status',timeout=15)
                            if not eligible(status) or status.get('machine')!=host['machine']:raise RuntimeError('Worker identity or accepting policy changed')
                            metrics=client.call('metrics',timeout=15)
                            free=metrics.get('ram_total_bytes',0)-metrics.get('ram_used_bytes',0)
                            if free>=1536*1024*1024 and metrics.get('percent',100)<85:break
                            with self.lock:self.state['jobs'][item['id']]['status']='waiting_capacity';self.save()
                            if time.monotonic()>deadline:raise RuntimeError('Worker capacity wait expired')
                            time.sleep(5)
                        with self.lock:self.state['jobs'][item['id']]['status']='running';self.save()
                    finally:client.close()
                    result=execute(host,item)
                    write_json(self.folder/(item['id']+'.json'),result)
                    with self.lock:
                        self.results.append(result);self.state['jobs'][item['id']].update(status='passed',elapsed_seconds=result['elapsed_seconds'],input_bytes=result['input_bytes']);self.save()
                except Exception as error:
                    with self.lock:self.state['jobs'][item['id']].update(status='failed',reason=type(error).__name__);self.save()
            finally:self.queue.task_done()

    def run(self,sources,limit):
        threads=[threading.Thread(target=self.worker,args=(host,),daemon=True) for host in self.hosts for _ in range(self.slots)]
        for thread in threads:thread.start()
        try:
            for kind,path in sources.items():
                stat=path.stat();self.state['sources'][kind]={'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}
                for batch in batches(path,kind,limit):
                    with self.lock:self.state['jobs'][batch['id']]={'kind':kind,'status':'queued','records':batch['records']};self.save()
                    self.queue.put(batch)
                if path.stat().st_mtime_ns!=stat.st_mtime_ns or path.stat().st_size!=stat.st_size:raise RuntimeError('Input changed during analysis')
            self.queue.join()
            if any(j['status']!='passed' for j in self.state['jobs'].values()):raise RuntimeError('Some private batches failed; inspect private state.json')
            for kind in sources:write_json(self.folder/(kind+'-summary.json'),merge(self.results,kind))
            self.state['status']='passed'
        except Exception:
            self.queue.join();self.state['status']='failed';raise
        finally:
            for thread in threads:self.queue.put(None)
            for thread in threads:thread.join()
            self.state['finished']=datetime.datetime.now(datetime.timezone.utc).isoformat();self.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qc',type=Path,required=True);parser.add_argument('--annotations',type=Path,required=True)
    parser.add_argument('--slots',type=int,choices=[1,2],default=2)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--batch-records',type=int,default=50000)
    args=parser.parse_args();os.umask(0o077);folder=private_run(args.output)
    print('Private distributed run: '+str(folder),flush=True)
    run=Run(discover(),folder,args.slots);run.save()
    run.run({'qc':args.qc.resolve(strict=True),'annotation':args.annotations.resolve(strict=True)},args.batch_records)
    print('Distributed genomics run passed',flush=True)


if __name__=='__main__':main()
