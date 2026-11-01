"""Signed, bounded-memory parallel BAM QC over the existing enrolled mTLS peers.

No input cache is created. A complete compressed-input MD5 and successful full
BAM/CRC parse are mandatory before a receipt is committed. Network retries
restart the stream, because parser state is not checkpointed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

STOP = threading.Event()


def load_worker(root):
    spec = importlib.util.spec_from_file_location('authorized_qc_peer', root/'worker.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def streaming_recipe(code):
    needle = "gzip.open('/input','rb')"
    if code.count(needle) != 1:
        raise ValueError('Unsupported authorized BAM parser')
    result = 'import sys\n'+code.replace(needle, "gzip.GzipFile(fileobj=sys.stdin.buffer,mode='rb')")
    compile(result, '<streaming-bam-qc>', 'exec')
    return ['python3', '-c', result]


def pump(response, destination, expected_bytes, expected_md5, progress=lambda n:None, stop=STOP):
    """One reusable MiB buffer; no persistence or whole-input allocation."""
    buffer = bytearray(1024*1024); view = memoryview(buffer)
    count = 0; digest = hashlib.md5(); updated = 0
    while size := response.readinto(buffer):
        if stop.is_set():raise InterruptedError('Stream stopped')
        count += size
        if count > expected_bytes:raise ValueError('Stream exceeded authorized input length')
        digest.update(view[:size]); destination.write(view[:size])
        if time.monotonic()-updated >= 2:
            progress(count); updated = time.monotonic()
    if count != expected_bytes or digest.hexdigest() != expected_md5:
        raise ValueError('Complete streamed input failed publisher length/MD5 verification')
    destination.flush(); progress(count)
    return count


def validate_stream_receipt(value,job,manifest,node,output_sha256):
    expected={'schema':'venter-stream-result.v1','run':manifest['id'],'parent_run':manifest['parent_run'],
              'job':job['id'],'node':node,'input_md5':job['spec']['source_md5'],
              'input_bytes':job['spec']['bytes'],'output_sha256':output_sha256}
    if any(value.get(k)!=v for k,v in expected.items()):raise ValueError('Saved stream receipt identity changed')
    if value.get('module_sha256') not in manifest.get('accepted_modules',[manifest['module_sha256']]):
        raise ValueError('Saved stream receipt code was not authorized')


class Offload:
    def __init__(self, root, delegation, registry):
        self.root=root; self.worker=load_worker(root); self.peer=self.worker.Peer(root)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        enrollment=self.worker.load(Path.home()/'.config/lifetech-launcher/peers.json')
        self.manifest=self.worker.verified(self.worker.load(delegation),Ed25519PublicKey.from_public_bytes(bytes.fromhex(enrollment['controller_public_key'])))
        m=self.manifest
        if (m['schema']!='venter-stream-offload.v1' or m['parent_run']!=self.peer.manifest['id']
            or m['worker']!=self.peer.name or m['module_sha256']!=self.worker.digest(Path(__file__))
            or not m['created']<=time.time()<=m['expires'] or not 1<=len(m['jobs'])<=64):
            raise ValueError('Invalid or expired stream authorization')
        if m.get('shared_input_cache'):
            cache=m['shared_input_cache']
            if (m.get('cache_module_sha256')!=self.worker.digest(Path(__file__).with_name('run_input_cache.py'))
                or type(cache.get('disk_bytes')) is not int or not 4*1024**2<=cache['disk_bytes']<=2*1024**3):
                raise ValueError('Invalid shared input cache authorization')
        self.parallelism=m.get('parallel_streams',2)
        if not 1<=self.parallelism<=8:raise ValueError('Invalid stream parallelism')
        if m.get('source_port'):
            self.peer.manifest['nodes'][m['source']]={**self.peer.manifest['nodes'][m['source']],'port':m['source_port'],'address':m['source_address']}
        if m.get('cache_peers'):
            if m.get('peer_selector_sha256')!=self.worker.digest(Path(__file__).with_name('run_cache_peers.py')):
                raise ValueError('Peer selector code differs from authorization')
            for name,endpoint in m['cache_peers'].items():
                if name not in self.peer.manifest['nodes']:raise ValueError('Unenrolled cache peer')
        for job in m['jobs']:
            if job['spec']['kind']!='bam-read-qc' or hashlib.sha256(self.worker.canonical(job['spec'])).hexdigest()!=job['id']:
                raise ValueError('Stream job identity changed')
        spec=importlib.util.spec_from_file_location('lease',root/'lease.py')
        self.lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.lease)
        self.folder=root/'stream-offload'/m['id'];self.folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.instance=(self.folder/'run.lock').open('a');fcntl.flock(self.instance,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.retries=self.worker.RetryLedger(self.folder);self.retries.recover()
        self.registry=registry;self.lock=threading.Lock()
        self.state={'id':m['id'],'name':'HuRef 2.0 — parallel streamed read quality on CompAI','status':'running','parent_run':m['parent_run'],'input_mode':'stream','parallelism':self.parallelism,
                    'detail':'Two direct mTLS streams; bounded memory; full publisher MD5 and BAM CRC/structure required. NIC DMA, not RDMA.',
                    'children':[{'id':j['id'],'name':'Read quality — '+j['spec']['path'],'worker':m['worker'],'status':'queued',
                                 'total_units':j['spec']['bytes'],'completed_units':0,'unit':'input bytes','input':j['spec']['path'],'detail':'Direct authenticated stream; no full input cache. Completion requires full MD5 and BAM CRC validation.'} for j in m['jobs']]}
        self.save()

    def save(self):
        self.state['updated']=time.time();self.worker.atomic(self.folder/'state.json',self.state)
        self.registry.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.worker.atomic(self.registry,self.state)

    def update(self, ident, **values):
        with self.lock:
            next(j for j in self.state['children'] if j['id']==ident).update(values);self.save()

    def execute(self, job):
        ident=job['id'];spec=job['spec'];folder=self.folder/ident;folder.mkdir(mode=0o700,exist_ok=True)
        receipt=folder/'receipt.json'
        if receipt.exists():
            value=self.worker.verified(self.worker.load(receipt),self.peer.keys[self.peer.name])
            validate_stream_receipt(value,job,self.manifest,self.peer.name,self.worker.digest(folder/'result.json'))
            self.update(ident,status='completed',completed_units=spec['bytes']);return
        name='venter-stream-'+ident[:24];process=None;lease=None
        try:
            while not STOP.is_set():
                if time.time()>self.manifest['expires']:raise PermissionError('Stream authorization expired; no new execution')
                policy=self.lease.worker_policy(Path.home()/'.config/lifetech-launcher/peers.json')
                if not policy['accepting_jobs']:
                    self.update(ident,status='waiting',detail=policy['reason']);STOP.wait(5);continue
                lease=self.lease.ResourceLease('venter-stream-qc',2,2048,name,directory=self.peer.config['resource_directory'],pids=64)
                if lease.try_acquire():break
                lease.close();lease=None;self.update(ident,status='waiting',detail='Waiting for signed local CPU/RAM admission');STOP.wait(5)
            if STOP.is_set():raise InterruptedError('Stopped before admission')
            attempt=self.retries.begin(ident)
            self.update(ident,attempt=attempt)
            command=streaming_recipe(self.worker.BAM_QC)
            argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.venter-stream='+self.manifest['id'],
                  '--network','none','--read-only','--log-driver','none','--cpus','2','--memory','2048m','--memory-swap','2048m',
                  '--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0',
                  '--user',f'{os.getuid()}:{os.getgid()}','-i',self.peer.node['image'],*command]
            source=self.manifest['source'];started=time.time()
            self.update(ident,status='running',phase='streaming and parsing',completed_units=0,attempt_started=started)
            with (folder/'result.partial').open('wb') as stdout,(folder/'stderr.txt').open('wb') as stderr:
                process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr)
                if self.manifest.get('shared_input_cache'):
                    from scripts.run_input_cache import InputCache, CachedInput
                    from scripts.run_cache_peers import PeerRanges
                    ranges=PeerRanges(self.peer,self.manifest,spec['source_md5'],spec['bytes'])
                    cache=InputCache(Path.home()/'.cache/swarm-control/inputs',
                                     budget=self.manifest['shared_input_cache']['disk_bytes'])
                    response=CachedInput(cache,spec['source_md5'],spec['bytes'],ranges)
                    pump(response,process.stdin,spec['bytes'],spec['source_md5'],
                         lambda n:self.update(ident,completed_units=n,phase='shared cached stream and parsing'))
                else:
                    with self.peer.open_peer(source,'/objects/'+spec['source_md5'],timeout=60) as response:
                        if response.status!=200 or int(response.headers['Content-Length'])!=spec['bytes']:
                            raise ValueError('Source response differs from signed input')
                        pump(response,process.stdin,spec['bytes'],spec['source_md5'],
                             lambda n:self.update(ident,completed_units=n,phase='streaming and parsing'))
                process.stdin.close()
                if process.wait(timeout=180)!=0:raise ValueError('BAM parser/CRC validation failed; incomplete result retained')
            result=self.worker.load(folder/'result.partial')
            if result.get('counts',{}).get('records',0)<=0:raise ValueError('Empty or invalid BAM QC result')
            (folder/'result.partial').replace(folder/'result.json')
            value={'schema':'venter-stream-result.v1','run':self.manifest['id'],'parent_run':self.manifest['parent_run'],
                   'job':ident,'node':self.peer.name,'input_md5':spec['source_md5'],'input_bytes':spec['bytes'],
                   'input_validation':'full streamed compressed-input MD5, exact length and complete BAM/gzip CRC parse',
                   'module_sha256':self.manifest['module_sha256'],'image':self.peer.node['image'],
                   'output_sha256':self.worker.digest(folder/'result.json'),'elapsed_seconds':time.time()-started}
            self.worker.atomic(receipt,self.worker.signed(value,self.peer.key))
            self.retries.complete(ident)
            self.update(ident,status='completed',phase='verified',completed_units=spec['bytes'])
        finally:
            if process is not None:
                removed=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
                check=subprocess.run(['docker','inspect','--format','{{.State.Running}}',name],capture_output=True,text=True,timeout=10)
                if check.returncode==0 and check.stdout.strip()=='true':
                    if lease:lease.file.close();lease=None
                    raise RuntimeError('Stream container cleanup unconfirmed; admission retained')
                if process.poll() is None:process.kill();process.wait()
            if lease:lease.close()

    def job(self, job):
        while not STOP.is_set():
            row=self.retries.states().get(job['id'],{})
            if row.get('state') in ('blocked','exhausted'):
                self.update(job['id'],status='failed',detail=row.get('reason'));return
            if row.get('next_due',0) and row['next_due']>time.time():
                if STOP.wait(min(5,row['next_due']-time.time())):return
                continue
            try:self.execute(job);return
            except (OSError,subprocess.SubprocessError) as error:
                state,due=self.retries.fail(job['id'],str(error),isinstance(error,PermissionError))
                self.update(job['id'],status='failed' if state in ('blocked','exhausted') else 'waiting',
                            detail=str(error)[:200],retry={'state':state,'next_due':due,'attempts':self.retries.states()[job['id']]['attempts']})
            except Exception as error:
                self.retries.fail(job['id'],str(error),True)
                self.update(job['id'],status='failed',detail=str(error)[:200]);return

    def run(self):
        with ThreadPoolExecutor(max_workers=self.parallelism) as pool:list(pool.map(self.job,self.manifest['jobs']))
        self.state['status']='completed' if all(j['status']=='completed' for j in self.state['children']) else 'failed';self.save()


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--delegation',type=Path,required=True)
    p.add_argument('--registry',type=Path,required=True);args=p.parse_args();os.umask(0o077)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    Offload(args.root,args.delegation,args.registry).run()

if __name__=='__main__':main()
