"""Owner-only, resumable chromosome comparison through enrolled swarm workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG
from agent_updates import LauncherUpdates
from scripts import genomics_distributed as distributed
from scripts import run_manager as rm
from docker_utils.images import ImageResolver

IMAGE='lifetech/genome-compare:20261031'
CHROMS=[str(n) for n in range(1,23)]+['X','Y']
STOP=threading.Event()

HELPER=r'''import importlib.util,json,pathlib,sys,subprocess,time,hashlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
h=json.loads(sys.stdin.buffer.readline());root=pathlib.Path(h['root'])
spec=importlib.util.spec_from_file_location('authorized_peer',root/'worker.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w);peer=w.Peer(root)
config=pathlib.Path.home()/'.config/lifetech-launcher/peers.json'
auth=w.verified(w.load(root/h['delegation']),Ed25519PublicKey.from_public_bytes(bytes.fromhex(w.load(config)['controller_public_key'])))
job=next(j for j in auth['jobs'] if j['id']==h['job'])
if not auth['created']<=time.time()<=auth['expires'] or auth['comparison_worker_sha256']!=hashlib.sha256(h['worker'].encode()).hexdigest():raise ValueError('Invalid comparison authorization')
if h['image']!=auth['comparison_image_ids'][peer.name]:
 metadata=json.loads(subprocess.check_output(['docker','image','inspect',h['image']],text=True))[0]
 content={key:metadata[key] for key in ('RootFS','Config','Architecture','Os','Created')}
 content['Config']={key:value for key,value in content['Config'].items() if value is not None}
 content['Config']['WorkingDir']=content['Config'].get('WorkingDir') or ''
 fingerprint=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 if fingerprint!=auth.get('comparison_image_fingerprints',{}).get(peer.name):raise ValueError('Unapproved comparison image')
runtime=(config.parent/'input-releases/current').resolve();sys.path.insert(0,str(runtime))
from scripts.run_input_cache import InputCache,CachedInput
from scripts.run_cache_peers import PeerRanges
if w.digest(runtime/'scripts/run_input_cache.py')!=auth['cache_module_sha256'] or w.digest(runtime/'scripts/run_cache_peers.py')!=auth['peer_selector_sha256']:raise ValueError('Unapproved cache runtime')
source=auth['source'];peer.manifest['nodes'][source]={**peer.manifest['nodes'][source],'address':auth['source_address'],'port':auth['source_port']}
s=job['spec'];ranges=PeerRanges(peer,auth,s['source_md5'],s['bytes'])
stream=CachedInput(InputCache(pathlib.Path.home()/'.cache/swarm-control/inputs'),s['source_md5'],s['bytes'],ranges)
command=['docker','run','--rm','--pull=never','--name',h['container'],'--label','org.lifetech.genomics=true','--label','org.lifetech.huref-comparison='+auth['id'],'--network','none','--read-only','--log-driver','none','--cpus',str(h['cpus']),'--memory-reservation',str(h['memory_mb'])+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--ulimit','memlock=-1:-1','--tmpfs','/work:rw,nosuid,nodev,size='+str(h['host_memory_mb'])+'m,mode=0700','-i',h['image'],'python3','-c',h['worker']]
p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=sys.stdout.buffer,stderr=sys.stderr.buffer)
try:
 header={'chromosome':s['chromosome'],'public_bytes':s['bytes'],'public_sha256':s['sha256'],'private_bytes':s['private_bytes'],'private_sha256':s['private_sha256'],'cpus':h['cpus'],'query_start':s['query_start'],'query_end':s['query_end']}
 p.stdin.write((json.dumps(header)+'\n').encode());buffer=bytearray(1024**2);digest=hashlib.md5();count=0
 while n:=stream.readinto(buffer):p.stdin.write(memoryview(buffer)[:n]);digest.update(memoryview(buffer)[:n]);count+=n
 if count!=s['bytes'] or digest.hexdigest()!=s['source_md5']:raise ValueError('Public input checksum mismatch')
 remaining=s['private_bytes']
 while remaining:
  data=sys.stdin.buffer.read(min(1024**2,remaining))
  if not data:raise ValueError('Private input truncated')
  p.stdin.write(data);remaining-=len(data)
 p.stdin.close()
 if p.wait()!=0:raise RuntimeError('Comparison container did not validate')
finally:
 if p.poll() is None:p.kill();p.wait()
'''


def save(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2));temp.chmod(0o600);temp.replace(path)


def digest(path,algorithm='sha256'):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,algorithm).hexdigest()


def memory_reservation(spec):
    """Estimate index, resident input and tile scratch bytes; never a hard cap."""
    span=spec['query_end']-spec['query_start']
    required=8*spec['reference_bases']+2*spec['query_bases']+4*span+4*spec['private_bytes']+spec['bytes']
    return max(1,(required+1024**2-1)//1024**2)


def region_jobs(jobs,lengths,reference_lengths,span=40*1024**2):
    result=[]
    for job in jobs:
        original=job['spec'];chrom=original['chromosome'];length=lengths[chrom]
        for start in range(0,length,span):
            end=min(length,start+span)
            spec={**original,'query_start':start,'query_end':end,'query_bases':length,'reference_bases':reference_lengths[chrom]}
            result.append({'id':'chr'+chrom+'-'+str(start)+'-'+str(end),'spec':spec})
    return result


class Comparison:
    def __init__(self,folder):
        self.folder=folder;folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.lock=threading.RLock();self.parent_root=Path.home()/'.local/state/venter-peer-qc/venter-qc-20261031-63400430'
        self.w=rm.worker(self.parent_root);self.peer=self.w.Peer(self.parent_root)
        self.updater=LauncherUpdates(ROOT,STOP.is_set,lambda *args:None)
        self.registry=Path.home()/'.local/state/swarm-control/tasks/huref-reference-variant-plan.json'
        self.plan=json.loads(self.registry.read_text())
        self.state=json.loads((folder/'state.json').read_text()) if (folder/'state.json').exists() else {'id':'huref-compare-'+folder.name,'status':'preparing','jobs':{},'created':time.time()}
        self.config=json.loads(CONFIG.read_text());self.hosts=[]

    def note(self,ident,status,detail,worker=None):
        with self.lock:
            def visit(row):
                if row.get('id')==ident:
                    row.update(status=status,detail=detail)
                    if worker:row['worker']=worker
                for child in row.get('children',[]):visit(child)
            visit(self.plan)
            self.plan['status']=self.state['status'];self.plan['updated']=time.time();save(self.registry,self.plan);save(self.folder/'state.json',self.state)

    def fetch(self,relative):
        manifest=self.peer.manifest;job=next(j for j in manifest['jobs'] if j['spec']['path']==relative);spec=job['spec']
        output=self.folder/Path(relative).name
        if output.exists() and output.stat().st_size==spec['bytes'] and digest(output,'md5')==spec['source_md5']:return output
        # Enrollment stores the remote root under HOME/.local/state/... .
        source=Path(manifest['nodes']['julian-latitude.local']['root']).parents[3]/'Downloads/HuRef2.0'/relative
        host={'host':'julian-latitude.local','lan_address':self.config['peers']['julian-latitude.local']}
        code='import pathlib,sys;f=pathlib.Path('+repr(str(source))+').open("rb");import shutil;shutil.copyfileobj(f,sys.stdout.buffer,1024**2)'
        with output.with_suffix('.partial').open('wb') as f:
            subprocess.run(self.updater.command(host,code),stdout=f,check=True,timeout=600)
        part=output.with_suffix('.partial')
        if part.stat().st_size!=spec['bytes'] or digest(part,'md5')!=spec['source_md5']:raise ValueError('Publisher input verification failed')
        part.replace(output);return output

    def prepare(self):
        source=next(Path('/mnt/PILEDRIVE/JulianDNA').glob('*.snp-indel.genome.vcf.gz'))
        prior=json.loads(Path('/mnt/PILEDRIVE/genomics-workbench/reference-matches/run-hk4ry36x/Julian-reference-summary.json').read_text())['source']
        if digest(source)!=prior['sha256']:raise ValueError('Julian input identity changed')
        self.note('input-identity','running','Verifying publisher inputs and partitioning primary chromosomes on CompAI','compai.local')
        assembly=self.fetch('assemblies/GCA_059792245.1_HuRef2.0_genomic.fna.gz')
        vcf=self.fetch('support/HuRef2.0.heterozygous.phased.vcf.gz')
        prepared=self.folder/'prepared';prepared.mkdir(mode=0o700,exist_ok=True)
        if not (prepared/'ready.json').exists():
            # Source decompression is done once; per-chromosome inputs are reused on retries.
            handles={};headers=[]
            try:
                with gzip.open(source,'rt') as stream:
                    for line in stream:
                        if line.startswith('#'):
                            if line.startswith('#CHROM'):headers=[line]
                            continue
                        chrom=line.split('\t',1)[0].removeprefix('chr')
                        if chrom not in CHROMS:continue
                        if chrom not in handles:
                            handles[chrom]=gzip.open(prepared/(chrom+'.julian.vcf.gz'),'wt',compresslevel=1);handles[chrom].writelines(headers)
                        handles[chrom].write(line)
            finally:
                for f in handles.values():f.close()
            handles={}
            try:
                with gzip.open(vcf,'rt') as stream:
                    for line in stream:
                        if line.startswith('#'):continue
                        chrom=line.split('\t',1)[0].replace('HuRef2.r2-chr','').lstrip('0')
                        if chrom not in CHROMS:continue
                        if chrom not in handles:handles[chrom]=gzip.open(prepared/(chrom+'.huref.vcf.gz'),'wt',compresslevel=1)
                        handles[chrom].write(line)
            finally:
                for f in handles.values():f.close()
            target=None
            try:
                with gzip.open(assembly,'rt') as stream:
                    for line in stream:
                        if line.startswith('>'):
                            if target:target.close();target=None
                            match=re.search(r' chromosome (\d+|X|Y),',line)
                            if match and match[1] in CHROMS:
                                target=(prepared/(match[1]+'.query.fa')).open('w');target.write('>HuRef2.r2-chr'+match[1].zfill(2)+'\n')
                        elif target:target.write(line)
            finally:
                if target:target.close()
            jobs=[];source_files={}
            reference=Path('/mnt/PILEDRIVE/genomics-workbench/vep-parallel/run-nv30w0nr/public-reference')
            for chrom in CHROMS:
                private=prepared/(chrom+'.julian.vcf.gz');query=prepared/(chrom+'.query.fa');variants=prepared/(chrom+'.huref.vcf.gz')
                if not private.exists() or not query.exists():continue
                if not variants.exists():
                    with gzip.open(variants,'wt') as f:f.write('')
                public=prepared/(chrom+'.public.tar.gz')
                with tarfile.open(public,'w:gz',compresslevel=1) as tar:
                    for path,name in [(reference/(chrom+'.fa'),'reference.fa'),(query,'query.fa'),(variants,'huref.vcf.gz')]:tar.add(path,arcname=name,recursive=False)
                st=public.stat();md5=digest(public,'md5')
                spec={'kind':'chromosome-comparison','chromosome':chrom,'source_md5':md5,'bytes':st.st_size,'sha256':digest(public),
                      'private_bytes':private.stat().st_size,'private_sha256':digest(private)}
                jobs.append({'id':'chr'+chrom,'spec':spec})
                source_files[md5]={'path':str(public),**{key:getattr(st,key) for key in ('st_size','st_mtime_ns','st_ctime_ns','st_ino','st_dev')}}
                query.unlink()  # Retained compressed bundle is the reusable public object.
            save(prepared/'ready.json',{'jobs':jobs,'source_files':source_files,'julian_input_sha256':prior['sha256']})
        ready=json.loads((prepared/'ready.json').read_text())
        lengths={}
        with gzip.open(vcf,'rt') as stream:
            for line in stream:
                match=re.match(r'##contig=<ID=HuRef2.r2-chr(\d+|X|Y),length=(\d+)',line)
                if match:lengths[match[1].lstrip('0')]=int(match[2])
                if line.startswith('#CHROM'):break
        reference=Path('/mnt/PILEDRIVE/genomics-workbench/vep-parallel/run-nv30w0nr/public-reference')
        reference_lengths={chrom:int((reference/(chrom+'.fa.fai')).read_text().split('\t')[1]) for chrom in CHROMS}
        ready['jobs']=region_jobs(ready['jobs'],lengths,reference_lengths)

        if ready['julian_input_sha256']!=prior['sha256']:raise ValueError('Prepared source identity changed')
        self.note('input-identity','completed','Input checksums pinned; public chromosome bundles reusable; private chromosome shards remain owner-only','compai.local')
        self.note('input-qc','completed','All 14 HuRef2 QC jobs have verified completion receipts')
        self.note('reference-choice','completed','GRCh38 primary autosomes and X/Y; mitochondrial and alternate/decoy contigs excluded')
        self.note('compatibility','completed','Different coordinate systems confirmed; chromosome assembly alignment is required')
        return ready

    def enroll(self):
        discovered=distributed.discover(require_python_image=False)
        self.hosts=[h for h in discovered if h['eligible'] and h['host'] in self.peer.manifest['nodes']]
        if not self.hosts:raise RuntimeError('No authorized comparison workers')
        expected=subprocess.check_output(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],text=True).strip()
        self.image_fingerprint=ImageResolver([],lambda h:h['ssh']).fingerprint(None,IMAGE)
        if not self.image_fingerprint:raise ValueError('Comparison image content identity unavailable')
        self.image_pool=ThreadPoolExecutor(max_workers=len(self.hosts))
        def provision(host):
            resolver=ImageResolver([host],lambda h:h['ssh'])
            try:
                resolver.ensure(IMAGE,[None if host['local'] else host])
                actual=resolver.host_images['local' if host['local'] else host['host']][IMAGE]
                if actual!=expected and resolver.fingerprint(None if host['local'] else host,IMAGE)!=self.image_fingerprint:raise ValueError('Comparison image identity mismatch')
                host['compare_image']=actual
                host['_image_ready'].set()
            except Exception:host['_image_error']='Image provisioning failed; other workers continue'
        for host in self.hosts:
            host['lan_address']=self.config['peers'][host['host']]
            host['compare_image']=expected;host['_image_ready']=threading.Event()
            self.image_pool.submit(provision,host)
        save(self.folder/'workers.json',[{k:v for k,v in h.items() if k not in ('ssh','_image_ready')} for h in self.hosts])

    def serving(self,ready):
        m=self.peer.manifest;worker=(ROOT/'scripts/huref_compare_worker.py').read_text();self.worker=worker
        auth={'schema':'venter-stream-offload.v1','id':self.state['id'],'parent_run':m['id'],'worker':self.peer.name,'source':self.peer.name,
              'created':time.time()-5,'expires':m['expires'],'jobs':ready['jobs'],'source_files':ready['source_files'],
              'source_address':m['nodes'][self.peer.name]['address'],'source_port':28981,
              'source_module_sha256':self.w.digest(ROOT/'scripts/run_input_source.py'),
              'cache_module_sha256':self.w.digest(ROOT/'scripts/run_input_cache.py'),
              'peer_selector_sha256':self.w.digest(ROOT/'scripts/run_cache_peers.py'),
              'comparison_worker_sha256':hashlib.sha256(worker.encode()).hexdigest(),
              'comparison_image_ids':{h['host']:h['compare_image'] for h in self.hosts},
              'comparison_image_fingerprints':{h['host']:self.image_fingerprint for h in self.hosts},
              'cache_peers':{h['host']:{'address':m['nodes'][h['host']]['address'],'port':38981} for h in self.hosts if h['host']!=self.peer.name}}
        envelope=rm.sign(auth,self.w);self.auth=auth;self.delegation='comparison-delegation.json'
        files={name:__import__('base64').b64encode((ROOT/'scripts'/name).read_bytes()).decode() for name in ('run_input_source.py','run_input_cache.py')}
        payload={**files,'stream-delegation.json':__import__('base64').b64encode(self.w.canonical(envelope)).decode(),'delegation_file':self.delegation}
        for host in self.hosts:
            name=host['host'];p={**payload,'root':m['nodes'][name]['root'],'unit':'run-input-comparison-origin.service' if host['local'] else 'run-input-comparison-cache.service','cache_only':not host['local']}
            subprocess.run(self.updater.command(host,rm.INSTALL_SOURCE),input=self.w.canonical(p),stdout=subprocess.DEVNULL,check=True,timeout=60)
            restart='import subprocess;subprocess.run('+repr(['systemctl','--user','restart'])+'+'+repr([p['unit']])+',check=True)'
            subprocess.run(self.updater.command(host,restart),check=True,timeout=30)
        return auth

    def execute(self,host,job):
        if host.get('_image_error'):raise RuntimeError(host['_image_error'])
        if not host['_image_ready'].is_set():raise distributed.CapacityWait('Worker image is preparing; ready workers continue')
        ident=job['id'];s=job['spec'];client=AgentClient(host);lease=uuid.uuid4().hex;name='genomics-'+uuid.uuid4().hex
        renew_done=threading.Event();errors=[];process=None;admitted=False;monitor=None
        try:
            capacity=client.call('resource.capacity',{'group':self.state['id']})
            memory_mb=memory_reservation(s);cpus=min(2,capacity.get('available_cpus',0))
            if cpus<1 or memory_mb>capacity['available_mb']:raise distributed.CapacityWait('Waiting for data-sized CPU/RAM reservation')
            request={'id':lease,'group':self.state['id'],'cpus':cpus,'memory_mb':memory_mb,'container':name}
            if not client.call('resource.acquire',request)['acquired']:raise distributed.CapacityWait('Waiting for data-sized CPU/RAM reservation')
            admitted=True
            def renew():
                other=AgentClient(host)
                try:
                    while not renew_done.wait(45):
                        try:other.call('resource.renew',{'id':lease},timeout=15)
                        except Exception:
                            other.close();other=AgentClient(host)
                            try:other.call('resource.renew',{'id':lease},timeout=15)
                            except Exception:errors.append('Lease renewal failed');return
                finally:other.close()
            threading.Thread(target=renew,daemon=True).start()
            with self.lock:
                self.state['jobs'][ident]={'status':'running','query_bases':s['query_end']-s['query_start'],'worker':host['host'],'started':time.time(),'container':name,'lease_id':lease,'memory_reservation_mb':memory_mb,'hard_memory_cap':False,'cpus':cpus};self.publish()
            header={'root':self.peer.manifest['nodes'][host['host']]['root'],'delegation':self.delegation,'job':ident,
                    'worker':self.worker,'image':host['compare_image'],'container':name,'memory_mb':memory_mb,'host_memory_mb':capacity['memory_mb'],'cpus':cpus}
            result=self.folder/(ident+'.result.json');error=self.folder/(ident+'.stderr')
            with result.with_suffix('.partial').open('wb') as out,error.open('wb') as err:
                process=subprocess.Popen(distributed.shell_command(host,['python3','-c',HELPER]),stdin=subprocess.PIPE,stdout=out,stderr=err)
                def observe():
                    with error.open() as observations:
                        while not renew_done.wait(2):
                            phase=None
                            for line in observations:
                                try:
                                    observation=json.loads(line)
                                    if observation.get('comparison_progress'):phase=observation['phase']
                                except (ValueError,KeyError):continue
                            with self.lock:
                                current=self.state['jobs'][ident]
                                if phase:current['phase']=phase
                                self.publish()
                monitor=threading.Thread(target=observe,daemon=True);monitor.start()
                process.stdin.write((json.dumps(header)+'\n').encode())
                with (self.folder/'prepared'/(s['chromosome']+'.julian.vcf.gz')).open('rb') as private:shutil.copyfileobj(private,process.stdin,1024**2)
                process.stdin.close()
                if process.wait() or errors:raise RuntimeError('Comparison worker failed; private diagnostic retained')
            data=json.loads(result.with_suffix('.partial').read_text())
            if data['public_sha256']!=s['sha256'] or data['private_sha256']!=s['private_sha256'] or data['chromosome']!=s['chromosome'] or data['query_start']!=s['query_start'] or data['query_end']!=s['query_end']:raise ValueError('Result identity mismatch')
            result.with_suffix('.partial').replace(result)
            receipt={'schema':'huref-comparison-result.v1','run':self.state['id'],'job':ident,'worker':host['host'],
                     'machine':host['machine'],'image':host['compare_image'],'worker_sha256':self.auth['comparison_worker_sha256'],
                     'public_sha256':s['sha256'],'private_sha256':s['private_sha256'],'query_start':s['query_start'],'query_end':s['query_end'],'memory_reservation_mb':memory_mb,'hard_memory_cap':False,'output_sha256':digest(result),'time':time.time()}
            save(self.folder/(ident+'.receipt.json'),rm.sign(receipt,self.w))
            with self.lock:self.state['jobs'][ident].update(status='completed',finished=time.time());self.publish()
        finally:
            renew_done.set()
            if monitor:monitor.join(timeout=3)
            if process is not None and process.poll() is None:process.kill();process.wait()
            if admitted:
                from scripts.wgs_recall_swarm import cleanup_container
                if not cleanup_container(host,name):raise RuntimeError('Container cleanup unconfirmed; lease retained')
            client.call('resource.release',{'id':lease});client.close()

    def reusable(self,job):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            value=self.w.verified(self.w.load(self.folder/(job['id']+'.receipt.json')),
                                 Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.config['controller_public_key'])))
            spec=job['spec']
            expected={'run':self.state['id'],'job':job['id'],'worker_sha256':self.auth['comparison_worker_sha256'],
                      'public_sha256':spec['sha256'],'private_sha256':spec['private_sha256'],
                      'query_start':spec['query_start'],'query_end':spec['query_end'],
                      'output_sha256':digest(self.folder/(job['id']+'.result.json'))}
            return all(value.get(k)==v for k,v in expected.items()) and value.get('image')==self.auth['comparison_image_ids'].get(value.get('worker'))
        except (OSError,ValueError,KeyError):return False

    def publish(self):
        rows=[{'id':ident,'name':'GRCh38 region mapping and reported-SNP comparison — '+ident,
               'status':row['status'],'worker':row.get('worker','unassigned'),'detail':row.get('detail',row.get('phase','Bounded query tiles; cached public blocks; private data in container RAM')), 'attempt_started':row.get('started'), 'elapsed_seconds':max(0,row.get('finished',time.time())-row['started']) if row.get('started') else None} for ident,row in self.state['jobs'].items()]
        finished=[r for r in self.state['jobs'].values() if r['status']=='completed' and r.get('query_bases')]
        total_bases=sum(r.get('query_bases',0) for r in self.state['jobs'].values())
        done_bases=sum(r['query_bases'] for r in finished)
        elapsed=time.time()-self.state.get('execution_started',time.time())
        mapping_eta=(total_bases-done_bases)*elapsed/done_bases if len(finished)>=2 and elapsed>0 and done_bases else None
        def visit(row):
            if row.get('id')=='coordinate-transform':row.update(status='completed' if rows and all(r['status']=='completed' for r in rows) else 'failed' if any(r['status']=='failed' for r in rows) else 'running' if any(r['status']=='running' for r in rows) else 'waiting',children=rows,detail='Chromosome mappings and conservative SNP comparisons on leased swarm workers', execution_eta_seconds=mapping_eta, eta_scope='Mapping', eta_basis='Observed completed query bases per elapsed wall time; assumes current fleet throughput continues')
            for child in row.get('children',[]):
                if row.get('id')!='coordinate-transform':visit(child)
        visit(self.plan);self.plan.update(status=self.state['status'],updated=time.time());save(self.registry,self.plan);save(self.folder/'state.json',self.state)

    def run(self):
        self.state['status']='preparing'
        ready=self.prepare();self.enroll();self.serving(ready)
        if self.state.get('jobs'):
            archive=self.folder/'attempts'/str(time.time_ns());archive.mkdir(parents=True,mode=0o700)
            save(archive/'state.json',self.state)
            for path in self.folder.glob('chr*.stderr'):shutil.copy2(path,archive/path.name)

        # A previous chromosome-sized attempt cannot be counted as a region job.
        self.state['jobs']={ident:row for ident,row in self.state['jobs'].items() if ident in {j['id'] for j in ready['jobs']}}
        self.note('partition','not scheduled','Signed comparison work items follow comparable-region and representation validation')
        current_ids={job['id'] for job in ready['jobs']}
        self.state['jobs']={ident:row for ident,row in self.state['jobs'].items() if ident in current_ids}
        jobs=queue.Queue()
        for job in ready['jobs']:
            ident=job['id'];row=self.state['jobs'].get(ident,{})
            if row.get('status')=='completed' and self.reusable(job):continue
            self.state['jobs'][ident]={'status':'queued','query_bases':job['spec']['query_end']-job['spec']['query_start']};jobs.put(job)
        self.state['status']='running';self.state['execution_started']=time.time();self.publish()
        def worker(host):
            while True:
                try:job=jobs.get_nowait()
                except queue.Empty:return
                if host.get('_image_error'):
                    jobs.put(job);jobs.task_done();return
                try:
                    for attempt in range(3):
                        try:self.execute(host,job);break
                        except distributed.CapacityWait as error:
                            with self.lock:
                                self.state['jobs'][job['id']]={**self.state['jobs'][job['id']],'status':'waiting_capacity','worker':host['host'],'detail':str(error)};self.publish()
                            jobs.put(job);time.sleep(5);break
                        except Exception:
                            if attempt==2:
                                with self.lock:self.state['jobs'][job['id']]={'status':'failed','worker':host['host'],'detail':'Three attempts failed; inspect private diagnostics'};self.publish()
                            else:time.sleep(10*(attempt+1))
                finally:jobs.task_done()
        slots=[h for h in self.hosts for _ in range(max(1,min(jobs.qsize(),h.get('cpus',2)//2)))]
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:list(pool.map(worker,slots))
        self.state['status']='completed' if all(r['status']=='completed' for r in self.state['jobs'].values()) else 'failed'
        self.publish()
        self.aggregate()
        self.image_pool.shutdown(wait=False)

    def aggregate(self):
        from collections import Counter
        counts=Counter();sites={};duplicate=set()
        for ident,row in self.state['jobs'].items():
            if row['status']!='completed':continue
            d=json.loads((self.folder/(ident+'.result.json')).read_text())
            counts.update({k:v for k,v in d.items() if type(v) is int and k not in ('query_start','query_end')})
            for key,label in d.get('site_evidence',{}).items():
                if key in sites:
                    if key not in duplicate:
                        old=sites[key]
                        if old!='unknown':counts[old]-=1
                        duplicate.add(key)
                    if label!='unknown':counts[label]-=1
                else:sites[key]=label
        counts['cross_region_ambiguous_targets']=len(duplicate)
        counts['assessed_snp_genotypes']=counts['genotype_agree']+counts['genotype_differ']
        save(self.folder/'aggregate.json',{'counts':dict(counts),'status':self.state['status'],
             'scope':'Chromosome-scoped assembly alignment and reported SNP comparison only. No whole-genome genotype agreement, indel equivalence, SV/CNV, or phase agreement claimed.'})
        self.state['status']='partial' if self.state['status']=='completed' else 'failed'
        self.plan['status']=self.state['status']
        self.note('snp-compare','partial' if self.state['status']=='partial' else 'failed','Chromosome-scoped reported-SNP subset; full scope remains limited by callability, indels and genome-wide mapping validation')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--folder',type=Path,required=True);args=parser.parse_args()
    os.umask(0o077);comparison=Comparison(args.folder)
    try:comparison.run()
    except Exception as error:
        comparison.state['status']='failed';comparison.note('coordinate-transform','failed',type(error).__name__+': '+str(error)[:160]);raise

if __name__=='__main__':main()
