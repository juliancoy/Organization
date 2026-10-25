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
        result=subprocess.run(command,input=batch['payload'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
        if result.returncode:raise RuntimeError('Worker failed ('+str(result.returncode)+'): '+result.stderr.decode(errors='replace')[:400])
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
    def __init__(self,hosts,folder,slots=2,resume=False):
        self.hosts=[h for h in hosts if h['eligible']];self.folder=folder;self.lock=threading.RLock();self.slots=slots
        self.queue=queue.Queue();self.credits=threading.Semaphore(max(2,len(self.hosts)*slots*2));self.results=[]
        if not self.hosts:raise RuntimeError('No authenticated, accepting workers with pinned SSH access')
        digest=hashlib.sha256(WORKER.encode()).hexdigest()
        previous=json.loads((folder/'state.json').read_text()) if resume else {}
        if resume and previous.get('worker_source_sha256')!=digest:raise ValueError('Resume requires matching scientific worker code')
        self.previous_sources=previous.get('sources',{})
        self.state={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'running',
                    'previous_started':previous.get('started'),'worker_source_sha256':digest,
                    'workers':[{k:v for k,v in h.items() if k not in ('ssh','agent_endpoint')} for h in hosts],
                    'jobs':{},'sources':{},'scope':'Private streamed QC and existing-annotation audit; no fresh VEP run. Remote inputs remain in process memory only.'}
        for host in self.hosts:host['_capacity_lock']=threading.Lock();host['_checked']=0;host['_cooldown']=0

    def save(self):
        with self.lock:
            temporary=self.folder/'state.tmp';temporary.write_text(json.dumps(self.state,indent=2));temporary.chmod(0o600);temporary.replace(self.folder/'state.json')

    def capacity(self,host):
        with host['_capacity_lock']:
            if time.monotonic()<host['_cooldown']:return False
            if time.monotonic()-host['_checked']<3:return host.get('_available',False)
            from agent_client import AgentClient
            client=AgentClient(host);available=False
            try:
                status=client.call('status',timeout=15)
                if eligible(status) and status.get('machine')==host['machine']:
                    metrics=client.call('metrics',timeout=15)
                    free=metrics.get('ram_total_bytes',0)-metrics.get('ram_used_bytes',0)
                    available=free>=1536*1024*1024 and metrics.get('percent',100)<85
            except (OSError,RuntimeError,ValueError):pass
            finally:client.close()
            host['_available']=available;host['_checked']=time.monotonic();return available

    def cached(self,batch):
        path=self.folder/(batch['id']+'.json')
        if not path.exists():return None
        result=json.loads(path.read_text())
        if any(result.get(key)!=batch[key] for key in ['sha256','records','kind']):raise ValueError('Cached batch differs from current input')
        return result

    def worker(self,host):
        while True:
            item=self.queue.get();finished=False
            try:
                if item is None:return
                if time.monotonic()>item['deadline']:
                    with self.lock:self.state['jobs'][item['id']].update(status='failed',reason='No accepting worker before deadline');self.save()
                    finished=True;continue
                if not self.capacity(host) or (host['host'] in item.get('failed_hosts',[]) and len(set(item['failed_hosts']))<len(self.hosts)):
                    with self.lock:self.state['jobs'][item['id']].update(status='waiting_capacity');self.save()
                    self.queue.put(item);time.sleep(2);continue
                with self.lock:self.state['jobs'][item['id']].update(status='running',host=host['host']);self.save()
                try:
                    result=execute(host,item);write_json(self.folder/(item['id']+'.json'),result)
                    with self.lock:
                        self.results.append(result);self.state['jobs'][item['id']].update(status='passed',elapsed_seconds=result['elapsed_seconds'],input_bytes=result['input_bytes']);self.save()
                    finished=True
                except Exception as error:
                    item.setdefault('failed_hosts',[]).append(host['host']);host['_cooldown']=time.monotonic()+15
                    final=len(item['failed_hosts'])>=3
                    with self.lock:self.state['jobs'][item['id']].update(status='failed' if final else 'retrying',reason=type(error).__name__+': '+str(error)[:500],attempts=len(item['failed_hosts']));self.save()
                    if final:finished=True
                    else:self.queue.put(item)
            finally:
                if finished:self.credits.release()
                self.queue.task_done()

    def run(self,sources,limit):
        threads=[threading.Thread(target=self.worker,args=(host,),daemon=True) for host in self.hosts for _ in range(self.slots)]
        for thread in threads:thread.start()
        try:
            for kind,path in sources.items():
                stat=path.stat();identity={'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}
                if kind in self.previous_sources and identity!=self.previous_sources[kind]:raise ValueError('Input identity changed since original run')
                self.state['sources'][kind]=identity
                for batch in batches(path,kind,limit):
                    cached=self.cached(batch)
                    with self.lock:
                        self.state['jobs'][batch['id']]={'kind':kind,'status':'passed' if cached else 'queued','records':batch['records']}
                        if cached:
                            self.results.append(cached);self.state['jobs'][batch['id']].update(host=cached['host'],reused=True,elapsed_seconds=cached['elapsed_seconds'])
                        self.save()
                    if not cached:
                        self.credits.acquire();batch['deadline']=time.monotonic()+1800;self.queue.put(batch)
                if path.stat().st_mtime_ns!=stat.st_mtime_ns or path.stat().st_size!=stat.st_size:raise RuntimeError('Input changed during analysis')
            self.queue.join()
            if not self.state['jobs'] or any(j['status']!='passed' for j in self.state['jobs'].values()):raise RuntimeError('Some private batches failed; inspect private state.json')
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
    parser.add_argument('--resume',type=Path,help='Resume a private run with matching input and scientific code')
    parser.add_argument('--slots',type=int,choices=[1,2],default=2)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--batch-records',type=int,default=50000)
    args=parser.parse_args();os.umask(0o077)
    if args.resume:
        folder=args.resume.absolute()
        if folder.is_symlink() or folder.parent.resolve()!=args.output.resolve() or folder.stat().st_uid!=os.getuid() or folder.stat().st_mode&0o077:raise ValueError('Resume requires an owner-only run under the output directory')
        if any((parent/'.git').exists() for parent in [folder,*folder.parents]):raise ValueError('Private runs cannot be inside Git')
    else:folder=private_run(args.output)
    print('Private distributed run: '+str(folder),flush=True)
    run=Run(discover(),folder,args.slots,resume=bool(args.resume));run.save()
    run.run({'qc':args.qc.resolve(strict=True),'annotation':args.annotations.resolve(strict=True)},args.batch_records)
    print('Distributed genomics run passed',flush=True)


if __name__=='__main__':main()
