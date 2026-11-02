"""Shared swarm-node execution and verified transfer runtime.

No application imports, scientific algorithms, sample identities or fixed budgets.
Applications declare resources, mounts, scratch and labels. The runtime enforces
admission/isolation, observes lifecycle events, and cleans only its job container.
"""
import datetime
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
SERVER=ROOT/'swarm-control/server'
if str(SERVER) not in sys.path:sys.path.insert(0,str(SERVER))
from agent_client import AgentClient,CONFIG


class CapacityWait(RuntimeError):
    """Execution is waiting for owner permission or reserved resources."""


def shell_command(host,command):
    if host['local']:return command
    return host['ssh'][:-1]+['-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',host['ssh'][-1],shlex.join(command)]


def eligible(status):
    return bool(status.get('ready') and status.get('protocol')=='lifetech-agent.v1'
                and status.get('worker_policy',{}).get('accepting_jobs'))


def discover(required_image=None, names=None):
    from concurrent.futures import ThreadPoolExecutor
    from docker_utils.policy import inventory,ssh
    from docker_utils.images import ImageResolver
    config=json.loads(CONFIG.read_text());accounts={h['host']:h for h in inventory()}
    resolver=ImageResolver([],lambda h:h['ssh'])
    expected=resolver.fingerprint(None,required_image) if required_image else None
    if required_image and not expected:raise RuntimeError('Approved local requested image unavailable')
    def check(item):
        name,address=item
        host={'host':name,'local':name==config['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
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
            if required_image:
                image=subprocess.check_output(shell_command(host,['docker','image','inspect',required_image,'--format','{{.Id}}']),text=True,stderr=subprocess.DEVNULL,timeout=20).strip()
                fingerprint=resolver.fingerprint(None if host['local'] else host,required_image)
                if fingerprint!=expected or not image.startswith('sha256:'):raise RuntimeError('Worker does not have the approved requested image contents')
                host['image_fingerprint']=fingerprint
                host['image']=image
            host['eligible']=True;host['machine']=status['machine'];host['cpus']=client.call('metrics',timeout=15).get('cpus') or 2
            if 'shared-resources' not in status.get('capabilities',[]):raise RuntimeError('Worker needs the shared resource scheduler update')
        except Exception as error:host.update(eligible=False,reason=type(error).__name__+': '+str(error)[:240])
        finally:
            if client:client.close()
        return host
    peers=[item for item in config['peers'].items() if names is None or item[0] in names]
    if not peers:return []
    with ThreadPoolExecutor(max_workers=min(8,len(peers))) as pool:return list(pool.map(check,peers))


def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path,value):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2));temporary.chmod(0o600);temporary.replace(path)


def validate_job(cpus,memory_mb,group,tmpfs,labels,container_prefix):
    if type(cpus) is not int or type(memory_mb) is not int or min(cpus,memory_mb)<1:raise ValueError('Expected positive integral CPU/RAM budgets')
    if not isinstance(group,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',group):raise ValueError('Expected a job group identifier')
    if container_prefix not in (None,'genomics','swarm-job'):raise ValueError('Unsupported job container prefix')
    if tmpfs is not None and not isinstance(tmpfs,dict):raise ValueError('RAM scratch must be a mount-to-budget mapping')
    if labels is not None and not isinstance(labels,dict):raise ValueError('Application labels must be a mapping')
    total=0
    for target,size_mb in (tmpfs or {}).items():
        if not isinstance(target,str) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+',target) or type(size_mb) is not int or size_mb<=0:raise ValueError('Invalid bounded RAM scratch mount')
        total+=size_mb
    if total>=memory_mb:raise ValueError('Combined RAM scratch must leave memory for the application')
    for key,value in (labels or {}).items():
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',key) or not isinstance(value,str):raise ValueError('Invalid application label')
        if key in ('org.lifetech.worker','org.lifetech.job','org.lifetech.group'):raise ValueError('Application cannot override runtime identity labels')


def cleanup_container(host,name):
    """Prove a job container absent even if its SSH transport already exited."""
    removed=subprocess.run(shell_command(host,['docker','rm','-f',name]),capture_output=True,text=True,timeout=30)
    if removed.returncode==0:return True
    inspected=subprocess.run(shell_command(host,['docker','container','inspect',name]),capture_output=True,text=True,timeout=20)
    return inspected.returncode==1 and ('No such object:' in inspected.stderr or 'No such container:' in inspected.stderr)


def run_job(host,image,command,mounts,work,cpus,memory_mb,report,*,group,wait_for_capacity=False,on_admitted=None,tmpfs=None,labels=None,container_prefix=None):
    """Docker RAM, including bounded tmpfs scratch, fits the acquired lease."""
    validate_job(cpus,memory_mb,group,tmpfs,labels,container_prefix)
    report=Path(report)
    lease=uuid.uuid4().hex;name=None;admission_requested=False
    client=AgentClient(host);done=threading.Event();errors=[];process=None
    try:
        status=client.call('status')
        if status.get('machine')!=host['machine']:raise RuntimeError('Worker identity changed')
        # Older enrolled agents require this legacy wire name. It does not
        # select a scientific workload; generic workers carry their own group.
        prefix=container_prefix or ('swarm-job' if 'generic-workers' in status.get('capabilities',[]) else 'genomics')
        name=prefix+'-'+lease
        if not eligible(status):raise CapacityWait('Worker is declining new work')
        if 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Renewable leases unavailable')
        capacity=client.call('resource.capacity',{'group':group})
        if not wait_for_capacity and (capacity['available_mb']<memory_mb or capacity.get('available_cpus',0)<cpus):raise CapacityWait('Insufficient current signed capacity')
        request={'id':lease,'group':group,'cpus':cpus,'memory_mb':memory_mb,'container':name}
        admission_requested=True
        result=client.call('resource.acquire',request)
        if not wait_for_capacity and not result['acquired']:raise CapacityWait('Capacity changed before reservation')
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        while not result['acquired']:
            atomic_json(report,{'phase':'waiting_capacity','host':host['host'],'cpus':cpus,'memory_mb':memory_mb,'updated':stamp()})
            time.sleep(2)
            status=client.call('status')
            if not eligible(status) or status.get('machine')!=host['machine']:raise CapacityWait('Worker is updating or declining new work')
            result=client.call('resource.acquire',request)
        if on_admitted:on_admitted()
        client.call('resource.renew',{'id':lease})
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(60):
                    for attempt in range(3):
                        try:
                            other.call('resource.renew',{'id':lease},timeout=10)
                            break
                        except Exception as error:
                            if isinstance(error,PermissionError) or attempt==2:
                                errors.append(type(error).__name__+': '+str(error))
                                subprocess.run(shell_command(host,['docker','kill',name]),capture_output=True,timeout=20)
                                return
                            other.close();other=AgentClient(host)
                            if done.wait(5*(attempt+1)):return
            finally:other.close()
        if 'docker-workers' not in status.get('capabilities',[]):threading.Thread(target=renew,daemon=True).start()
        argv=['docker','run','--pull=never','--name',name,'--label','org.lifetech.worker=true','--label','org.lifetech.job='+lease,'--label','org.lifetech.group='+group,'--network','none','--read-only','--log-driver','none','--cpus',str(cpus),'--memory',str(memory_mb)+'m','--memory-swap',str(memory_mb)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--ulimit','nofile=65536:65536','--user',host.get('container_user','1000:1000'),'-e','TMPDIR=/work/tmp','-w','/work']
        for key,value in (labels or {}).items():argv+=['--label',key+'='+value]
        for target,size_mb in (tmpfs or {}).items():
            argv+=['--tmpfs',target+':rw,nosuid,nodev,noexec,size='+str(size_mb)+'m,mode=1777']
        for source,target,readonly in mounts:
            argv+=['--mount','type=bind,src='+str(source)+',dst='+target+(',readonly' if readonly else '')]
        argv +=[image,'bash','-o','pipefail','-c',command]
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic_json(report,{'phase':'running','host':host['host'],'container':name,'lease_id':lease,'cpus':cpus,'memory_mb':memory_mb,'started':stamp(),'command':command,'image':image,'ram_scratch_mounts':tmpfs or {}})
        with report.with_suffix('.stdout').open('wb') as stdout,report.with_suffix('.stderr').open('wb') as stderr:
            process=subprocess.Popen(shell_command(host,argv),stdout=stdout,stderr=stderr)
            code=process.wait()
        detail={}
        if code or errors:
            try:
                inspected=subprocess.run(shell_command(host,['docker','inspect','--format','{{json .State}}',name]),capture_output=True,text=True,timeout=15)
                state=json.loads(inspected.stdout)
                detail={k:state.get(k) for k in ('Status','ExitCode','OOMKilled','Error','FinishedAt')}
            except (OSError,ValueError,subprocess.SubprocessError):
                detail={'inspection':'unavailable'}
        owner_paused=False
        if code or errors:
            try:owner_paused=client.call('worker.policy').get('accepting_jobs') is False
            except (OSError,RuntimeError,ValueError):pass
        result={'phase':'interrupted' if owner_paused else 'complete' if code==0 and not errors else 'failed','host':host['host'],'container':name,'exit_code':code,'finished':stamp(),'command':command,'image':image,'container_state':detail,'lease_errors':errors,'ram_scratch_mounts':tmpfs or {}}
        attempt=report.parent/'attempts'/(report.stem+'-'+name)
        attempt.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        result['attempt_report']=str(attempt.with_suffix('.json'))
        atomic_json(attempt.with_suffix('.json'),result)
        for suffix in ('.stdout','.stderr'):
            shutil.copyfile(report.with_suffix(suffix),attempt.with_suffix(suffix))
        atomic_json(report,result)
        if code or errors:
            # Owner stop/pause is a scheduling event, not a bad input or failed assay.
            if owner_paused:
                raise CapacityWait('Owner paused worker; requeue interrupted stage')
            raise RuntimeError('Worker stage failed: exit='+str(code)+' OOM='+str(detail.get('OOMKilled','unknown'))+'; evidence '+str(attempt.with_suffix('.json')))
    finally:
        done.set()
        cleaned=process is None
        try:
            if process is not None:cleaned=cleanup_container(host,name)
            if cleaned and admission_requested:client.call('resource.release',{'id':lease})
            elif cleaned:pass
            else:raise RuntimeError('Container cleanup unconfirmed; resource lease retained until authenticated expiry cleanup')
        finally:client.close()


def copy_verified_file(source,destination,receipt,resume=False):
    """Stream an authorized file over pinned SSH; verify SHA256 before publication."""
    if source['host']==destination['host']:return receipt
    client=AgentClient(source)
    try:
        status=client.call('status')
        if status.get('machine')!=source['machine']:raise RuntimeError('Source machine identity changed')
    finally:client.close()
    target=str(Path(destination['cache'])/'incoming'/receipt['sha256']/Path(receipt['path']).name)
    offset=0
    if resume:
        probe="import pathlib,sys;p=pathlib.Path(sys.argv[1]);print(p.stat().st_size if p.exists() else 0)"
        offset=int(subprocess.check_output(shell_command(destination,['python3','-c',probe,target+'.partial']),text=True,timeout=30))
        if not 0<=offset<=receipt['bytes']:raise ValueError('Invalid partial transfer size')
    writer="""import hashlib,json,os,pathlib,sys,time
path=pathlib.Path(sys.argv[1]);size=int(sys.argv[2]);expected=sys.argv[3]
metadata=json.loads(sys.argv[5]) if len(sys.argv)>5 else {};started=time.monotonic();last=started;last_bytes=0
progress=pathlib.Path.home()/'.local/state/lifetech-launcher/transfers'/metadata.get('id','unattributed')
progress=progress.with_suffix('.json')
try:progress.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
except OSError:progress=None
def publish(status,count,offset=0,force=False):
 global last,last_bytes
 now=time.monotonic();sent=max(0,count-offset)
 if progress is None or not force and now-last<1:return
 elapsed=max(.000001,now-started)
 row={**metadata,'status':status,'sampled_at':time.time(),'completed_bytes':count,'total_bytes':size,'transferred_bytes':sent,'bytes_per_second':max(0,sent-last_bytes)/max(.000001,now-last) if status=='running' else 0,'average_bytes_per_second':sent/elapsed,'elapsed_seconds':elapsed}
 try:
  tmp=progress.with_suffix('.tmp');tmp.write_text(json.dumps(row));tmp.chmod(0o600);tmp.replace(progress)
 except OSError:return
 last=now;last_bytes=sent
publish('preparing',0,force=True)
path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
if path.exists() and path.stat().st_size==size:
 h=hashlib.sha256()
 with path.open('rb') as s:
  for block in iter(lambda:s.read(8388608),b''):h.update(block)
 if h.hexdigest()==expected:publish('cached',size,size,force=True);sys.exit(0)
partial=path.with_suffix(path.suffix+'.partial');h=hashlib.sha256();n=0;offset=int(sys.argv[4])
if offset:
 if not partial.exists() or partial.stat().st_size!=offset:raise RuntimeError('Partial transfer changed before resume')
 with partial.open('rb') as prefix:
  for block in iter(lambda:prefix.read(8388608),b''):h.update(block);n+=len(block)
started=last=time.monotonic()
publish('running',n,offset,force=True)
with partial.open('ab' if offset else 'wb') as s:
 while block:=sys.stdin.buffer.read(1048576):
  n+=len(block)
  if n>size:raise RuntimeError('Read transfer exceeded declared size')
  h.update(block);s.write(block);publish('running',n,offset)
if n!=size:
 publish('interrupted',n,offset,force=True)
 raise ConnectionError('Read transfer incomplete; partial retained for final full-checksum validation on resume')
if h.hexdigest()!=expected:
 publish('failed',n,offset,force=True)
 partial.rename(partial.with_name(partial.name+'.mismatch-'+str(time.time_ns())))
 raise RuntimeError('Read transfer SHA256 mismatch')
partial.chmod(0o600);partial.replace(path)
publish('complete',n,offset,force=True)
# Keep bounded recent operational receipts; never retain source paths here.
if progress is not None:
 try:
  for old in sorted(progress.parent.glob('*.json'),key=lambda p:p.stat().st_mtime)[:-512]:old.unlink(missing_ok=True)
 except OSError:pass
"""
    reader="import shutil,sys;s=open(sys.argv[1],'rb');s.seek(int(sys.argv[2]));shutil.copyfileobj(s,sys.stdout.buffer,8388608)"
    source_command=['python3','-c',reader,receipt['path'],str(offset)] if offset else ['cat','--',receipt['path']]
    producer=subprocess.Popen(shell_command(source,source_command),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    consumer=None
    try:
        consumer=subprocess.Popen(shell_command(destination,['python3','-c',writer,target,str(receipt['bytes']),receipt['sha256'],str(offset),json.dumps({'id':uuid.uuid4().hex,'task_id':receipt.get('task_id') if isinstance(receipt.get('task_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',receipt['task_id']) else None,'source':source['host'],'destination':destination['host']})]),stdin=producer.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        producer.stdout.close()
        # Large verified files can take hours on a healthy LAN. SSH keepalives
        # detect broken transports; total elapsed time is not a transfer failure.
        _,errors=consumer.communicate()
        # A valid cached destination may exit before consuming a duplicate stream.
        producer.wait(timeout=20)
        if consumer.returncode:raise RuntimeError('LAN read transfer failed: '+errors.decode(errors='replace')[-300:])
    finally:
        for process in (consumer,producer):
            if process is not None:
                if process.poll() is None:process.kill();process.wait()
                if process.stderr:process.stderr.close()
    return {**receipt,'host':destination['host'],'path':target,'original_host':source['host']}
