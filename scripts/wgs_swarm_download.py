#!/usr/bin/env python3
"""Download public WGS directly to enrolled peers with resumable verified caches.

Only public manifest URLs are downloaded. Peer identity is authenticated before
SSH/container work, and each network container holds a renewable resource lease.
Full reads remain outside Git; coordinator state contains file receipts only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from scripts import genomics_distributed as distributed

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient

STOP=threading.Event()
GROUP='genomics-wgs-'+uuid.uuid4().hex[:16]
ALLOWED_HOSTS={'ftp.sra.ebi.ac.uk','ftp.ncbi.nlm.nih.gov','sra-download.ncbi.nlm.nih.gov'}
CHUNK_BYTES=128*1024*1024


def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def file_id(item):return hashlib.sha256(item['url'].encode()).hexdigest()[:20]


def validate_manifest(value):
    files=value.get('files')
    if not isinstance(files,list) or not files:raise ValueError('Manifest has no files')
    seen=set()
    for item in files:
        url=urlsplit(item['url'])
        if url.scheme!='https' or url.hostname not in ALLOWED_HOSTS or url.username or url.password or url.query or url.fragment:
            raise ValueError('Only expected public archive HTTPS URLs are accepted')
        if type(item['bytes']) is not int or item['bytes']<1:raise ValueError('Expected exact publisher byte count')
        if not re.fullmatch('[a-fA-F0-9]{32}',item['md5']):raise ValueError('Expected publisher MD5')
        if not re.fullmatch('[A-Za-z0-9_.-]+',item['filename']) or item['filename'] in ('.','..'):
            raise ValueError('Invalid public archive filename')
        if item['url'] in seen:raise ValueError('Duplicate manifest file')
        seen.add(item['url'])
    return files


def atomic_json(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2));temporary.chmod(0o600);temporary.replace(path)


PREFLIGHT=r'''import hashlib,json,os,pathlib,shutil,subprocess,sys
expected,requested=sys.argv[1:]
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=expected:raise RuntimeError('SSH identity differs from authenticated agent')
path=pathlib.Path(requested).expanduser().absolute()
if any(parent.is_symlink() for parent in [path,*path.parents]):raise RuntimeError('Symlink cache path rejected')
if any((parent/'.git').exists() for parent in [path,*path.parents]):raise RuntimeError('WGS cache must be outside Git')
path.mkdir(parents=True,exist_ok=True,mode=0o700)
if path.stat().st_uid!=os.getuid():raise RuntimeError('WGS cache ownership mismatch')
path.chmod(0o700)
security=json.loads(subprocess.check_output(['docker','info','--format','{{json .SecurityOptions}}'],text=True))
rootless=any('rootless' in item for item in security)
print(json.dumps({'cache':str(path),'free_bytes':shutil.disk_usage(path).free,'uid':os.getuid(),'gid':os.getgid(),'container_user':'0:0' if rootless else str(os.getuid())+':'+str(os.getgid()),'cache_sizes':{child.name:sum(item.stat().st_size for item in child.rglob('*') if item.is_file() and not item.is_symlink()) for child in path.iterdir() if child.is_dir() and not child.is_symlink()}}))
'''


# Self-contained stdlib worker. Chunk boundaries concern transport, not analysis.
DOWNLOAD_WORKER=r'''import concurrent.futures,contextlib,datetime,email.utils,fcntl,hashlib,itertools,json,os,pathlib,shutil,sys,threading,time,urllib.request,urllib.parse,urllib.error
job=json.load(sys.stdin);jobs=job.get('jobs',[job]);cache_root=pathlib.Path(os.environ.get('WGS_CACHE_ROOT','/cache'));guard=threading.RLock();finished=threading.Event();contexts={};errors=[];throttle_until=0;network_active=0;network_failures=0;network_bytes=0;network_limit=1;control_not_before=0;network_condition=threading.Condition(guard)

def save(path,value):
 tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value));tmp.chmod(0o600);tmp.replace(path)
def digest(path,algorithm):
 h=hashlib.new(algorithm)
 with path.open('rb') as stream:
  for data in iter(lambda:stream.read(8*1024*1024),b''):h.update(data)
 return h.hexdigest()
def emit(value):
 with guard:print(json.dumps(value),flush=True)
def report(ctx,phase=None):
 with guard:
  if phase:ctx['phase']=phase
  completed=sum(min(ctx['chunk_size'],ctx['item']['bytes']-i*ctx['chunk_size']) for i in ctx['done'])
  received=completed
  for index in ctx['active']:
   try:received+=(ctx['chunks']/(str(index)+'.part')).stat().st_size
   except FileNotFoundError:pass
  prefix=0
  while prefix in ctx['done']:prefix+=1
  save(ctx['folder']/'progress.json',{'contiguous_chunks':prefix,'next_required_chunk':prefix,'phase':ctx['phase'],'bytes_completed':completed,'bytes_received':received,'bytes_total':ctx['item']['bytes'],'transport_worker_version':ctx['transport_worker_version'],'completed_chunks':len(ctx['done']),'total_chunks':ctx['total'],'updated':time.time(),'network':{'active':network_active,'limit':network_limit,'failures':network_failures,'bytes_received':network_bytes,'retry_until':throttle_until}})
def initialize(current):
 item=current['item'];folder=cache_root/current['id'];folder.mkdir(mode=0o700,exist_ok=True)
 lock=(folder/'download.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 final=folder/item['filename'];receipt_path=folder/'receipt.json'
 if final.exists() and receipt_path.exists():
  prior=json.loads(receipt_path.read_text())
  if prior.get('url')==item['url'] and prior.get('md5')==item['md5'] and final.stat().st_size==item['bytes'] and prior.get('mtime_ns')==final.stat().st_mtime_ns:
   lock.close();emit(prior);return None
 chunks=folder/'chunks';chunks.mkdir(mode=0o700,exist_ok=True);chunk_size=current['chunk_bytes'];total=(item['bytes']+chunk_size-1)//chunk_size
 ctx={'id':current['id'],'item':item,'folder':folder,'final':final,'receipt_path':receipt_path,'chunks':chunks,'chunk_size':chunk_size,'total':total,'done':set(),'active':set(),'phase':'queued','lock':lock,'started':time.monotonic(),'remaining':total,'failed':False,'allowed_hosts':current['allowed_hosts'],'transport_worker_version':current.get('transport_worker_version','unversioned')}
 for index in range(total):
  target=chunks/(str(index)+'.bin');meta=chunks/(str(index)+'.json')
  if target.exists() and meta.exists():
   prior=json.loads(meta.read_text());expected=min(chunk_size,item['bytes']-index*chunk_size)
   if target.stat().st_size==expected and prior.get('sha256')==digest(target,'sha256'):ctx['done'].add(index)
 report(ctx);return ctx

class YieldNetworkSlot(Exception):pass
@contextlib.contextmanager
def network_slot(ctx,index):
 global network_active
 with network_condition:
  def prefix_blocked():
   if ctx['item'].get('layout')!='PAIRED':return False
   prefix=0
   while prefix in ctx['done']:prefix+=1
   return index>=prefix+max(1,network_limit)
  while network_active>=network_limit or time.time()<max(throttle_until,control_not_before) or prefix_blocked():network_condition.wait(.25)
  network_active+=1
 try:yield
 finally:
  with network_condition:network_active-=1;network_condition.notify_all()
def read_control():
 global network_limit,control_not_before
 path=cache_root/'.network-control.json'
 try:
  control=json.loads(path.read_text())
  with network_condition:
   network_limit=max(0,min(job['connections'],int(control['connections']))) if time.time()-control['updated']<20 else 0
   control_not_before=control.get('not_before',0);network_condition.notify_all()
 except FileNotFoundError:pass
def retry_delay(error,attempt):
 if isinstance(error,urllib.error.HTTPError) and error.code in (429,503):
  value=error.headers.get('Retry-After')
  if value:
   try:return max(0,float(value))
   except ValueError:
    try:return max(0,email.utils.parsedate_to_datetime(value).timestamp()-time.time())
    except (TypeError,ValueError):pass
 return min(2**attempt,30)
def fetch(ctx,index):
 global throttle_until,network_failures,network_bytes
 item=ctx['item'];start=index*ctx['chunk_size'];end=min(item['bytes'],start+ctx['chunk_size'])-1;target=ctx['chunks']/(str(index)+'.bin');meta=ctx['chunks']/(str(index)+'.json');partial=target.with_suffix('.part')
 while (ctx['folder']/'download.hold').exists():
  report(ctx,'waiting_for_seed');time.sleep(.25)
 if target.exists() and meta.exists():
  old=json.loads(meta.read_text())
  if target.stat().st_size==end-start+1 and old.get('sha256')==digest(target,'sha256'):
   with guard:ctx['done'].add(index)
   report(ctx);return
 with guard:ctx['active'].add(index);ctx['phase']='downloading'
 try:
  attempt=0
  while True:
   try:
    while time.time()<throttle_until:time.sleep(min(1,throttle_until-time.time()))
    existing=partial.stat().st_size if partial.exists() else 0
    if existing>end-start+1:raise ValueError('Invalid preserved partial range length')
    if existing<end-start+1:
     overlap=min(existing,1024*1024);effective_start=start+existing-overlap
     request=urllib.request.Request(item['url'],headers={'Range':f'bytes={effective_start}-{end}','User-Agent':'LifeTech-public-WGS/1'})
     with network_slot(ctx,index),urllib.request.urlopen(request,timeout=60) as response:
      remote=urllib.parse.urlsplit(response.geturl())
      if remote.scheme!='https' or remote.hostname not in ctx['allowed_hosts']:raise ValueError('Unexpected archive redirect')
      expected=f'bytes {effective_start}-{end}/{item["bytes"]}'
      if response.status!=206 or response.headers.get('Content-Range')!=expected:
       if not(ctx['total']==1 and effective_start==0 and response.status==200):raise ValueError('Archive did not honor exact byte range')
      if overlap:
       observed=response.read(overlap)
       with partial.open('rb') as previous:
        previous.seek(existing-overlap);expected_overlap=previous.read(overlap)
       if observed!=expected_overlap:
        quarantine=ctx['folder']/'integrity-quarantine';quarantine.mkdir(exist_ok=True,mode=0o700)
        partial.rename(quarantine/(partial.name+'.overlap-mismatch-'+str(time.time_ns())))
        raise ValueError('Preserved partial differs from official overlapping range')
      size=existing
      with partial.open('ab') as stream:
       while data:=response.read(min(1024*1024,end-start+1-size+1)):
        size+=len(data)
        if size>end-start+1:raise ValueError('Archive range exceeded expected length')
        stream.write(data)
        with guard:network_bytes+=len(data)
        if network_limit==0:raise YieldNetworkSlot()
      if size!=end-start+1:raise ValueError('Incomplete archive range')
    h=digest(partial,'sha256');partial.replace(target);save(meta,{'sha256':h,'bytes':end-start+1,'start':start,'end':end,'source_url':item['url'],'source_md5':item['md5'],'transport_worker_version':ctx['transport_worker_version']})
    with guard:ctx['done'].add(index)
    report(ctx);return
   except YieldNetworkSlot:continue
   except Exception as error:
    attempt+=1
    if attempt>=20:raise
    delay=retry_delay(error,attempt)
    if isinstance(error,(urllib.error.URLError,ConnectionError,TimeoutError)):
     with guard:network_failures+=1;throttle_until=max(throttle_until,time.time()+delay)
    time.sleep(delay)
 finally:
  with guard:ctx['active'].discard(index)

def assemble(ctx):
 item=ctx['item'];report(ctx,'assembling');assembled=ctx['final'].with_suffix(ctx['final'].suffix+'.assembling');md5=hashlib.md5();sha256=hashlib.sha256();size=0
 with assembled.open('wb') as output:
  for index in range(ctx['total']):
   with (ctx['chunks']/(str(index)+'.bin')).open('rb') as source:
    for data in iter(lambda:source.read(8*1024*1024),b''):
     md5.update(data);sha256.update(data);size+=len(data);output.write(data)
 if size!=item['bytes'] or md5.hexdigest().lower()!=item['md5'].lower():raise ValueError('Publisher checksum verification failed')
 assembled.replace(ctx['final']);ctx['final'].chmod(0o600)
 receipt={**item,'md5':md5.hexdigest(),'sha256':sha256.hexdigest(),'mtime_ns':ctx['final'].stat().st_mtime_ns,'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-ctx['started'],3),'cache_relative_path':str(ctx['final'].relative_to(cache_root))}
 save(ctx['receipt_path'],receipt);shutil.rmtree(ctx['chunks']);report(ctx,'verified');emit(receipt);ctx['lock'].close()

def execute(task):
 ctx,index=task
 try:
  if ctx['failed']:return
  fetch(ctx,index)
  with guard:ctx['remaining']-=1;complete=ctx['remaining']==0
  if complete:
   try:assemble(ctx)
   except ValueError as error:
    if str(error)!='Publisher checksum verification failed' or ctx['total']!=1:raise
    # Preserve bad transport evidence; a fresh single-range retry can repair a
    # corrupted preserved prefix without silently changing publisher identity.
    quarantine=ctx['folder']/('checksum-mismatch-'+str(time.time_ns()));quarantine.mkdir(mode=0o700)
    ctx['chunks'].replace(quarantine/'chunks');ctx['chunks'].mkdir(mode=0o700)
    duplicate=ctx['final'].with_suffix(ctx['final'].suffix+'.assembling')
    if duplicate.exists():duplicate.unlink()
    with guard:ctx['done'].clear();ctx['phase']='downloading'
    fetch(ctx,0);assemble(ctx)
 except Exception as error:
  with guard:
   if not ctx['failed']:
    ctx['failed']=True;errors.append(ctx['id']);report(ctx,'failed');emit({'error':type(error).__name__+': '+str(error)[:400],'id':ctx['id']})

def progress_loop():
 while not finished.wait(1):
  read_control()
  for ctx in list(contexts.values()):
   if ctx['phase'] in ('downloading','assembling'):report(ctx)

for current in jobs:
 try:
  ctx=initialize(current)
  if ctx:contexts[ctx['id']]=ctx
 except Exception as error:errors.append(current['id']);emit({'id':current['id'],'error':type(error).__name__+': '+str(error)[:400]})
read_control();threading.Thread(target=progress_loop,daemon=True).start()
# Give each paired stream its earliest missing compressed range before small
# files occupy all executor slots, then return to fair round-robin scheduling.
priority=[(ctx,next(i for i in range(ctx['total']) if i not in ctx['done'])) for ctx in contexts.values() if ctx['item'].get('layout')=='PAIRED' and len(ctx['done'])<ctx['total']]
priority_keys={(ctx['id'],index) for ctx,index in priority}
tasks=itertools.chain(priority,((ctx,index) for index in range(max((c['total'] for c in contexts.values()),default=0)) for ctx in contexts.values() if index<ctx['total'] and (ctx['id'],index) not in priority_keys))
try:
 with concurrent.futures.ThreadPoolExecutor(max_workers=job['connections']) as pool:list(pool.map(execute,tasks))
finally:
 finished.set()
 for ctx in contexts.values():ctx['lock'].close()
if errors:sys.exit(1)

'''


def preflight(host,local_cache):
    cache=str(local_cache) if host['local'] else '~/.local/state/lifetech-launcher/wgs-cache'
    result=subprocess.run(distributed.shell_command(host,['python3','-c',PREFLIGHT,host['machine'],cache]),capture_output=True,text=True,timeout=35)
    if result.returncode:raise RuntimeError('Worker WGS cache check failed: '+result.stderr[-300:])
    host={**host,**json.loads(result.stdout)}
    return host


def assign_files(files,hosts,previous=None):
    """Keep each FASTQ complete on one disk; reserve both chunk and final bytes."""
    previous=previous or {};by_name={h['host']:h for h in hosts};allocated={h['host']:0 for h in hosts};assignments={}
    for item in sorted(files,key=lambda row:row['bytes'],reverse=True):
        ident=file_id(item);old=previous.get(ident,{}).get('host')
        needed=lambda h:0 if old==h['host'] and previous.get(ident,{}).get('status')=='verified' else max(0,item['bytes']*2-h.get('cache_sizes',{}).get(ident,0))
        choices=[h for h in hosts if h['free_bytes']-allocated[h['host']]>=needed(h)]
        if old in by_name and by_name[old] in choices:chosen=by_name[old]
        elif choices:chosen=min(choices,key=lambda h:(allocated[h['host']]/max(1,h['free_bytes']),-h['free_bytes']))
        else:raise RuntimeError('Insufficient peer disk capacity for verified WGS cache')
        assignments[ident]=chosen['host'];allocated[chosen['host']]+=needed(chosen)
    return assignments


def cleanup_owned_container(host,name):
    if not re.fullmatch(r'genomics-[a-f0-9]{32}',name):raise ValueError('Invalid owned WGS container name')
    removed=subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20)
    if removed.returncode==0:return True
    check=subprocess.run(distributed.shell_command(host,['docker','inspect','--format','{{.State.Running}}',name]),capture_output=True,text=True,timeout=15)
    if check.returncode==0:return check.stdout.strip()!='true'
    return 'No such object' in check.stderr or 'No such container' in check.stderr


def release_after_cleanup(host,name,client,lease):
    # SSH may have died while its remote Docker container is still running.
    if not cleanup_owned_container(host,name):raise RuntimeError('Owned WGS container cleanup unconfirmed; lease retained for server expiry')
    client.call('resource.release',{'id':lease})


def run_batch(host,items,changed,connections):
    name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex;client=AgentClient(host);process=None;finished=threading.Event();renew_errors=[];received=set()
    by_id={file_id(item):item for item in items}
    try:
        status=client.call('status',timeout=10)
        if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise RuntimeError('Worker identity/policy changed')
        if 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Worker needs renewable-resource agent update')
        capacity=client.call('resource.capacity',{'group':GROUP});memory_mb=distributed.memory_allocation(capacity,max(1,host.get('cpus',1)))
        deadline=time.monotonic()+120
        while not client.call('resource.acquire',{'id':lease,'group':GROUP,'cpus':1,'memory_mb':memory_mb,'container':name})['acquired']:
            if STOP.wait(.5):raise InterruptedError('Download stopped')
            if time.monotonic()>deadline:raise distributed.CapacityWait('Waiting for worker capacity')
        client.call('resource.renew',{'id':lease})
        def renew():
            renewal_client=AgentClient(host)
            try:
                while not finished.wait(30):
                    try:renewal_client.call('resource.renew',{'id':lease},timeout=10)
                    except Exception as error:
                        renew_errors.append(str(error));subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20);return
            finally:renewal_client.close()
        threading.Thread(target=renew,daemon=True).start()
        payload={'jobs':[{'id':ident,'item':item,'chunk_bytes':CHUNK_BYTES,'allowed_hosts':sorted(ALLOWED_HOSTS),'transport_worker_version':'sha256:'+hashlib.sha256(DOWNLOAD_WORKER.encode()).hexdigest()} for ident,item in by_id.items()],'connections':connections}
        command=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.genomics=true','--label','org.lifetech.public-wgs=true','-i','--read-only','--log-driver','none','--cpus','1','--memory',str(memory_mb)+'m','--memory-swap',str(memory_mb)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--user',host['container_user'],'--mount','type=bind,src='+host['cache']+',dst=/cache',host['image'],'python','-c',DOWNLOAD_WORKER]
        for ident in by_id:changed(ident,'queued',{'memory_mb':memory_mb,'connections':connections})
        process=subprocess.Popen(distributed.shell_command(host,command),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def cancel():
            while not finished.wait(.5):
                if STOP.is_set():subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20);return
        def progress():
            code="import pathlib,json,sys;root=pathlib.Path(sys.argv[1]);out={};\nfor ident in json.loads(sys.argv[2]):\n p=root/ident/'progress.json'\n try:out[ident]=json.loads(p.read_text())\n except (OSError,ValueError):pass\nprint(json.dumps(out))"
            while not finished.wait(5):
                try:
                    result=subprocess.run(distributed.shell_command(host,['python3','-c',code,host['cache'],json.dumps(list(by_id))]),capture_output=True,text=True,timeout=20)
                    if result.returncode==0:
                        changed(None,None,{'host':host['host'],'progress_batch':json.loads(result.stdout)})
                except (OSError,ValueError,subprocess.SubprocessError):pass
        threading.Thread(target=cancel,daemon=True).start();threading.Thread(target=progress,daemon=True).start()
        process.stdin.write(json.dumps(payload).encode());process.stdin.close()
        for line in process.stdout:
            receipt=json.loads(line)
            if 'error' in receipt:
                changed(receipt['id'],'failed',{'error':receipt['error']});continue
            ident=file_id(receipt);item=by_id[ident]
            if receipt.get('url')!=item['url'] or receipt.get('bytes')!=item['bytes'] or receipt.get('md5','').lower()!=item['md5'].lower():raise ValueError('Download receipt identity mismatch')
            path=Path(host['cache'])/receipt['cache_relative_path']
            if not path.is_relative_to(host['cache']) or '..' in path.parts:raise ValueError('Invalid worker receipt path')
            receipt={**receipt,'host':host['host'],'path':str(path),'memory_mb':memory_mb};received.add(ident)
            changed(ident,'verified',{'receipt':receipt})
            print('Verified '+item['sample']+' '+item['filename']+' on '+host['host'],flush=True)
        process.wait();stderr=process.stderr.read()
        if renew_errors:raise RuntimeError('Download lease renewal failed')
        if process.returncode:raise RuntimeError('Public WGS download worker failed ('+str(process.returncode)+'): '+stderr.decode(errors='replace')[-500:])
        if received!=set(by_id):raise RuntimeError('Worker omitted download receipts')
    finally:
        finished.set()
        try:
            release_after_cleanup(host,name,client,lease)
        finally:
            if process is not None and process.poll() is None:process.kill();process.wait()
            client.close()



def flow_assignments(hosts,budget,rotation=0,priorities=None):
    if not hosts:return {}
    ordered=hosts[rotation%len(hosts):]+hosts[:rotation%len(hosts)]
    priorities=set(priorities or ());ordered=[h for h in ordered if h['host'] in priorities]+[h for h in ordered if h['host'] not in priorities]
    result={host['host']:0 for host in hosts}
    remaining=max(0,int(budget))
    while remaining:
        allocated=False
        for host in ordered:
            demand=min(24,max(1,host.get('cpus',1)))
            if result[host['host']]<demand:
                result[host['host']]+=1;remaining-=1;allocated=True
                if not remaining:break
        if not allocated:break
    return result


class FlowBudget:
    """Shared-WAN additive increase, multiplicative decrease after archive refusal."""
    def __init__(self,hosts,now=0):
        self.budget=max(1,len(hosts));self.maximum=sum(min(24,max(1,h.get('cpus',1))) for h in hosts)
        self.last_change=now;self.last_error=now-60;self.not_before=0;self.last_failures=0;self.previous_rate=None
    def update(self,now,failures,rate,retry_until=0):
        if failures>self.last_failures:
            self.budget=max(1,self.budget//2);self.last_error=now;self.last_change=now;self.not_before=max(now+45,retry_until);self.previous_rate=None
        elif now-self.last_error>=60 and now-self.last_change>=30 and rate>0 and (self.previous_rate is None or rate>self.previous_rate*1.03):
            self.previous_rate=rate;self.budget=min(self.maximum,self.budget+1);self.last_change=now
        self.last_failures=max(self.last_failures,failures)
        return self.budget


WRITE_CONTROL="""import pathlib,json,sys
p=pathlib.Path(sys.argv[1])/'.network-control.json';value=json.load(sys.stdin);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value));tmp.chmod(0o600);tmp.replace(p)
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/downloads'))
    parser.add_argument('--local-cache',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/cache'))
    parser.add_argument('--hosts',type=Path)
    parser.add_argument('--sample',action='append')
    parser.add_argument('--connections-per-host',type=int,default=0,help='0 derives simultaneous HTTP ranges from worker CPU capacity')
    args=parser.parse_args();os.umask(0o077)
    if args.connections_per_host<0:parser.error('--connections-per-host must be nonnegative')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:STOP.set())
    files=validate_manifest(json.loads(args.manifest.read_text()))
    if args.sample:files=[row for row in files if row['sample'] in args.sample]
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any((p/'.git').exists() for p in [args.output,*args.output.parents]):raise ValueError('Download state must be outside Git')
    lock=(args.output/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state_path=args.output/'state.json';previous=json.loads(state_path.read_text()) if state_path.exists() else {}
    candidates=json.loads(args.hosts.read_text()) if args.hosts else distributed.discover()
    hosts=[]
    for host in candidates:
        if not host.get('eligible'):continue
        try:hosts.append(preflight(host,args.local_cache))
        except Exception as error:print(host['host']+': '+str(error),flush=True)
    if not hosts:raise RuntimeError('No authenticated accepting worker has a writable cache')
    mapping=assign_files(files,hosts,previous.get('jobs'))
    state={'started':previous.get('started',stamp()),'status':'running','manifest_sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),'jobs':{},'workers':[{k:h[k] for k in ('host','cache','free_bytes','cpus')} for h in hosts]};guard=threading.RLock()
    items={file_id(item):item for item in files}
    for ident,item in items.items():state['jobs'][ident]={'host':mapping[ident],'sample':item['sample'],'run':item['run'],'bytes':item['bytes'],'filename':item['filename'],'layout':item.get('layout'),'status':'queued'}
    def save():
        with guard:state['updated']=stamp();atomic_json(state_path,state)
    save();print('Distributed WGS download state: '+str(state_path),flush=True)
    network_done=threading.Event();network_budget=FlowBudget(hosts,time.time());network_started=time.time();network_previous=[network_started,0];network_rates=[]
    if previous.get('network',{}).get('status')=='adaptive':network_budget.budget=min(network_budget.maximum,max(1,int(previous['network']['budget'])))
    def control(host,connections,not_before=0):
        value={'connections':connections,'updated':time.time(),'not_before':not_before}
        result=subprocess.run(distributed.shell_command(host,['python3','-c',WRITE_CONTROL,host['cache']]),input=json.dumps(value),text=True,capture_output=True,timeout=20)
        return result.returncode==0
    initial_priorities={row['host'] for row in previous.get('jobs',{}).values() if row.get('layout')=='PAIRED' and row.get('progress',{}).get('contiguous_chunks',0)==0}
    initial_allocations=flow_assignments(hosts,network_budget.budget,priorities=initial_priorities)
    with ThreadPoolExecutor(max_workers=len(hosts)) as controls:list(controls.map(lambda h:control(h,initial_allocations[h['host']]),hosts))
    def network_loop():
        while not network_done.wait(5):
            with guard:
                current=time.time();samples=state.get('network_workers',{});received=sum(v.get('bytes_received',0) for v in samples.values())
                failures=sum(v.get('failures',0) for v in samples.values());retry_until=max([0]+[v.get('retry_until',0) for v in samples.values()])
                elapsed=current-network_previous[0];rate=max(0,(received-network_previous[1])/elapsed) if elapsed>0 else 0;network_previous[:]=[current,received];network_rates.append(rate);network_rates[:]=network_rates[-6:]
                mean_rate=sum(network_rates)/len(network_rates);budget=network_budget.update(current,failures,mean_rate,retry_until)
                active_hosts=[h for h in hosts if not h.get('_download_finished')]
                pair_groups={}
                for row in state['jobs'].values():
                    if row.get('layout')=='PAIRED' and row['status']!='verified':pair_groups.setdefault(row['run'],[]).append(row)
                priorities=set()
                for rows in pair_groups.values():
                    minimum=min(row.get('progress',{}).get('contiguous_chunks',0) for row in rows)
                    priorities.update(row['host'] for row in rows if row.get('progress',{}).get('contiguous_chunks',0)==minimum)
                allocations=flow_assignments(active_hosts,budget,int((current-network_started)//30),priorities)
                state['network']={'budget':budget,'allocations':allocations,'rate_bytes_per_second':mean_rate,'archive_failures':failures,'not_before':network_budget.not_before,'status':'archive_backoff' if current<network_budget.not_before else 'adaptive','updated':current};save()
            with ThreadPoolExecutor(max_workers=max(1,len(hosts))) as controls:
                list(controls.map(lambda h:control(h,allocations.get(h['host'],0),network_budget.not_before),hosts))
    network_thread=threading.Thread(target=network_loop,daemon=True);network_thread.start()
    def worker(host):
        # Existing lease PID budget is32; leave room for main/progress/SSL threads.
        connections=min(args.connections_per_host or max(1,host.get('cpus',1)),24)
        selected=[ident for ident in items if mapping[ident]==host['host']]
        selected.sort(key=lambda ident:items[ident]['bytes'])
        def changed(ident,status,extra=None):
            with guard:
                if ident is None:
                    rows=extra['progress_batch']
                    network_rows=[row for row in rows.values() if 'network' in row]
                    if network_rows:state.setdefault('network_workers',{})[extra['host']]=max(network_rows,key=lambda row:row['updated'])['network']
                    for key,row in extra['progress_batch'].items():
                        if state['jobs'][key]['status'] not in ('verified','failed'):state['jobs'][key].update(status='queued' if row['phase']=='queued' else 'failed' if row['phase']=='failed' else 'downloading',progress=row)
                    save();return
                if state['jobs'][ident]['status']=='verified' and status!='verified':return
                state['jobs'][ident].update(status=status,**(extra or {}));save()
        try:run_batch(host,[items[ident] for ident in selected],changed,connections)
        except Exception as error:
            for ident in selected:
                if state['jobs'][ident]['status'] not in ('verified','failed'):changed(ident,'stopped' if STOP.is_set() else 'failed',{'error':type(error).__name__+': '+str(error)[:500]})
        host['_download_finished']=True
        return host['host']
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:list(pool.map(worker,hosts))
    network_done.set();network_thread.join(25)
    state['status']='stopped' if STOP.is_set() else 'passed' if all(row['status']=='verified' for row in state['jobs'].values()) else 'failed';state['finished']=stamp();save()
    print('Download status: '+state['status'],flush=True)
    if state['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
