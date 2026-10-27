#!/usr/bin/env python3
"""Renewable, authenticated execution for private WGS worker stages."""
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
from scripts import genomics_distributed as distributed
from scripts.wgs_swarm_download import AgentClient, atomic_json, stamp

IMAGE='lifetech/wgs-tools:bookworm-20261026'
GROUP='genomics-wgs-recall'


def cleanup_container(host,name):
    """Prove a job container absent even if its SSH transport already exited."""
    removed=subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,text=True,timeout=30)
    if removed.returncode==0:return True
    inspected=subprocess.run(distributed.shell_command(host,['docker','container','inspect',name]),capture_output=True,text=True,timeout=20)
    return inspected.returncode==1 and ('No such object:' in inspected.stderr or 'No such container:' in inspected.stderr)


def leased_run(host,image,command,mounts,work,cpus,memory_mb,report,wait_for_capacity=False,on_admitted=None):
    """All Docker CPU/RAM bounds exactly match the acquired signed lease."""
    name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex
    client=AgentClient(host);done=threading.Event();errors=[];process=None
    try:
        status=client.call('status')
        if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise RuntimeError('Worker identity/policy changed')
        if 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Renewable leases unavailable')
        capacity=client.call('resource.capacity',{'group':GROUP})
        if not wait_for_capacity and (capacity['available_mb']<memory_mb or capacity.get('available_cpus',0)<cpus):raise distributed.CapacityWait('Insufficient current signed capacity')
        request={'id':lease,'group':GROUP,'cpus':cpus,'memory_mb':memory_mb,'container':name}
        result=client.call('resource.acquire',request)
        if not wait_for_capacity and not result['acquired']:raise distributed.CapacityWait('Capacity changed before reservation')
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        while not result['acquired']:
            atomic_json(report,{'phase':'waiting_capacity','host':host['host'],'cpus':cpus,'memory_mb':memory_mb,'updated':stamp()})
            time.sleep(2)
            status=client.call('status')
            if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise distributed.CapacityWait('Worker is updating or declining new work')
            result=client.call('resource.acquire',request)
        if on_admitted:on_admitted()
        client.call('resource.renew',{'id':lease})
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(60):
                    try:other.call('resource.renew',{'id':lease},timeout=10)
                    except Exception as error:
                        errors.append(str(error));subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20);return
            finally:other.close()
        threading.Thread(target=renew,daemon=True).start()
        argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.genomics=true','--network','none','--read-only','--log-driver','none','--cpus',str(cpus),'--memory',str(memory_mb)+'m','--memory-swap',str(memory_mb)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--ulimit','nofile=65536:65536','--user',host.get('container_user','1000:1000'),'-e','TMPDIR=/work/tmp','-w','/work']
        for source,target,readonly in mounts:
            argv+=['--mount','type=bind,src='+str(source)+',dst='+target+(',readonly' if readonly else '')]
        argv +=[image,'bash','-o','pipefail','-c',command]
        report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic_json(report,{'phase':'running','host':host['host'],'container':name,'cpus':cpus,'memory_mb':memory_mb,'started':stamp(),'command':command,'image':image})
        with report.with_suffix('.stdout').open('wb') as stdout,report.with_suffix('.stderr').open('wb') as stderr:
            process=subprocess.Popen(distributed.shell_command(host,argv),stdout=stdout,stderr=stderr)
            code=process.wait()
        if errors:raise RuntimeError('Resource lease renewal failed')
        atomic_json(report,{'phase':'complete' if code==0 else 'failed','host':host['host'],'exit_code':code,'finished':stamp(),'command':command,'image':image})
        if code:raise RuntimeError('Worker stage failed; inspect private log '+str(report.with_suffix('.stderr')))
    finally:
        done.set()
        cleaned=process is None
        try:
            if process is not None:cleaned=cleanup_container(host,name)
            if cleaned:client.call('resource.release',{'id':lease})
            else:raise RuntimeError('Container cleanup unconfirmed; resource lease retained until authenticated expiry cleanup')
        finally:client.close()


def distribute_reference(host,reference,call_only=False):
    """LAN-only transfer followed by a full manifest SHA256 attestation."""
    import shlex
    if host['local']:return str(reference)
    target=str(Path(host['cache']).parent/'wgs-reference')
    subprocess.run(distributed.shell_command(host,['mkdir','-p','--',target]),check=True)
    # SSH options originate only in enrolled/pinned peer discovery.
    filters=['--include=reference.fa','--include=reference.fa.fai','--include=source.json','--exclude=*'] if call_only else []
    subprocess.run(['rsync','-a','--partial',*filters,'-e',shlex.join(host['ssh'][:-1]),str(reference)+'/',host['ssh'][-1]+':'+target+'/'],check=True,stdout=subprocess.DEVNULL)
    if call_only:
        from scripts.wgs_recall import digest
        source=json.loads((Path(reference)/'source.json').read_text())
        manifest={'files':{'reference.fa':{'bytes':(Path(reference)/'reference.fa').stat().st_size,'sha256':source['sha256']},'reference.fa.fai':{'bytes':(Path(reference)/'reference.fa.fai').stat().st_size,'sha256':digest(Path(reference)/'reference.fa.fai')}}}
    else:manifest=json.loads((Path(reference)/'attestation.json').read_text())
    check="""import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);manifest=json.loads(sys.argv[2])
for name,record in manifest['files'].items():
 f=p/name
 if f.stat().st_size!=record['bytes']:raise RuntimeError('Reference size mismatch')
 h=hashlib.sha256()
 with f.open('rb') as s:
  for block in iter(lambda:s.read(8*1024*1024),b''):h.update(block)
 if h.hexdigest()!=record['sha256']:raise RuntimeError('Reference SHA256 mismatch')
print('verified')
"""
    subprocess.run(distributed.shell_command(host,['python3','-c',check,target,json.dumps(manifest)]),check=True,stdout=subprocess.DEVNULL)
    return target


def map_watson(host,receipt,reference,image,report_folder):
    """Map one complete ENA run, retaining both short and long 454 reads."""
    import shlex
    from scripts.wgs_recall import alignment,pipeline
    client=AgentClient(host)
    try:capacity=client.call('resource.capacity',{'group':GROUP})
    finally:client.close()
    cpus=max(2,min(host['cpus']-1,capacity.get('available_cpus',0)))
    if capacity.get('available_cpus',0)<2 or capacity['available_mb']<7000:raise distributed.CapacityWait('Mapping awaits seven GiB and two CPUs')
    memory_mb=max(7000,capacity['available_mb']//2)
    work=str(Path(host['cache']).parent/'wgs-alignments'/receipt['run'])
    subprocess.run(distributed.shell_command(host,['mkdir','-p','--',work+'/tmp']),check=True)
    # Install exactly the reviewed pure-stdlib worker source in private workdir.
    source=(Path(__file__).parent/'wgs_recall.py').read_bytes()
    writer="import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)"
    subprocess.run(distributed.shell_command(host,['python3','-c',writer,work+'/runner.py']),input=source,check=True)
    setup="import importlib.util,json;from pathlib import Path;s=importlib.util.spec_from_file_location('recall','/work/runner.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.split_pilot_fastq('/input.fastq.gz','/work/reads',10**12)))"
    commands=['rm -f /work/reads/short.fastq /work/reads/long.fastq /work/long.bam /work/short.bam /work/short.sai /work/alignment.partial.bam /work/alignment.partial.bam.bai', 'python -c '+shlex.quote(setup)+' > /work/read-counts.json',
        alignment('/ref/reference.fa',['/work/reads/long.fastq'],'/work/long.bam','Watson',receipt['run'],cpus,memory_mb),
        'bwa aln -t '+str(cpus)+' /ref/reference.fa /work/reads/short.fastq > /work/short.sai',
        pipeline([['bwa','samse','-r','@RG\\tID:'+receipt['run']+'-short\\tSM:Watson\\tLB:Watson','/ref/reference.fa','/work/short.sai','/work/reads/short.fastq'],['samtools','sort','-m',str(max(128,memory_mb//4))+'M','-T','/work/tmp/sort','-o','/work/short.bam','-']]),
        'samtools merge /work/alignment.partial.bam /work/long.bam /work/short.bam',
        'samtools quickcheck /work/alignment.partial.bam','samtools index /work/alignment.partial.bam',
        'samtools flagstat -O json /work/alignment.partial.bam > /work/flagstat.json',
        'mv /work/alignment.partial.bam /work/alignment.bam','mv /work/alignment.partial.bam.bai /work/alignment.bam.bai']
    report=Path(report_folder)/(receipt['run']+'.json')
    leased_run(host,image,' && '.join(commands),[(reference,'/ref',True),(work,'/work',False),(receipt['path'],'/input.fastq.gz',True)],work,cpus,memory_mb,report)
    result={'host':host['host'],'run':receipt['run'],'bam':work+'/alignment.bam','source_sha256':receipt['sha256'],'reference':reference,'image':image,'scope':'Alignment only; Watson chromosome19 excluded from all downstream inference'}
    atomic_json(Path(report_folder)/(receipt['run']+'.receipt.json'),result)
    return result


def copy_receipt(source,destination,receipt):
    """Stream one verified public gzip over pinned SSH, SHA-check before rename."""
    if source['host']==destination['host']:return receipt
    client=AgentClient(source)
    try:
        status=client.call('status')
        if status.get('machine')!=source['machine']:raise RuntimeError('Source machine identity changed')
    finally:client.close()
    target=str(Path(destination['cache'])/'incoming'/receipt['sha256']/Path(receipt['path']).name)
    writer="""import hashlib,json,os,pathlib,sys
path=pathlib.Path(sys.argv[1]);size=int(sys.argv[2]);expected=sys.argv[3]
path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
if path.exists() and path.stat().st_size==size:
 h=hashlib.sha256()
 with path.open('rb') as s:
  for block in iter(lambda:s.read(8388608),b''):h.update(block)
 if h.hexdigest()==expected:sys.exit(0)
partial=path.with_suffix(path.suffix+'.partial');h=hashlib.sha256();n=0
with partial.open('wb') as s:
 while block:=sys.stdin.buffer.read(8388608):
  n+=len(block)
  if n>size:raise RuntimeError('Read transfer exceeded declared size')
  h.update(block);s.write(block)
if n!=size or h.hexdigest()!=expected:raise RuntimeError('Read transfer SHA256 mismatch')
partial.chmod(0o600);partial.replace(path)
"""
    producer=subprocess.Popen(distributed.shell_command(source,['cat','--',receipt['path']]),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        consumer=subprocess.Popen(distributed.shell_command(destination,['python3','-c',writer,target,str(receipt['bytes']),receipt['sha256']]),stdin=producer.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        producer.stdout.close()
        _,errors=consumer.communicate(timeout=1800)
        # A valid cached destination may exit before consuming a duplicate stream.
        producer.wait(timeout=20)
        if consumer.returncode:raise RuntimeError('LAN read transfer failed: '+errors.decode(errors='replace')[-300:])
    finally:
        if producer.poll() is None:producer.kill();producer.wait()
        if producer.stderr:producer.stderr.close()
    return {**receipt,'host':destination['host'],'path':target,'original_host':source['host']}


def watch(root):
    """Persistent host queues start each verified run as soon as capacity allows."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    root=Path(root);report=root/'recall/full';report.mkdir(mode=0o700,parents=True,exist_ok=True)
    hosts=json.loads((root/'recall-hosts.json').read_text());by_host={h['host']:h for h in hosts}
    strong=[h for h in hosts if h['host'] in ('compai.local','codecollective.local','julian-latitude.local','julian-inspiron-3793.local')]
    image=json.loads((root/'tools.json').read_text())['image']
    state_path=report/'state.json';state=json.loads(state_path.read_text()) if state_path.exists() else {'jobs':{}}
    # An interrupted invocation did not complete an active alignment.
    for value in state['jobs'].values():
        if value.get('phase')=='running':value['phase']='interrupted'
    guard=threading.Lock();claimed=set()
    atomic_json(report/'driver.json',{'phase':'waiting_for_validated_pilot','started':stamp()})
    while True:
        pilot=root/'recall/pilot/status.json'
        if pilot.exists():
            phase=json.loads(pilot.read_text()).get('phase')
            if phase=='failed':raise RuntimeError('Pilot failed; full queue remains stopped')
            if phase=='complete':break
        time.sleep(10)
    # Pilot must map the expected read count and yield actual aligned reads.
    flag=json.loads((root/'recall/pilot/flagstat.json').read_text())['QC-passed reads']
    if flag.get('primary',flag.get('total',0))<10000 or flag.get('mapped',0)<7000:raise RuntimeError('Pilot alignment validation failed')
    atomic_json(report/'driver.json',{'phase':'mapping_verified_runs','started':stamp(),'pilot_mapped':flag['mapped']})
    expected={row['run'] for row in json.loads((root/'catalog.json').read_text())['files'] if row['sample']=='Watson'}
    def worker(host):
        # Ensure only the fingerprint-verified LAN image is executed.
        from docker_utils.images import ImageResolver
        resolver=ImageResolver([h for h in hosts if not h['local']],lambda h:h['ssh'])
        resolver.ensure(IMAGE,[] if host['local'] else [host])
        worker_image=resolver.inspect(None if host['local'] else host,IMAGE)
        if not worker_image:raise RuntimeError('Verified bioimage missing on worker')
        reference=distribute_reference(host,root/'reference')
        while True:
            with guard:
                downloaded=json.loads((root/'downloads/state.json').read_text()).get('jobs',{})
                receipts=[v['receipt'] for v in downloaded.values() if v.get('sample')=='Watson' and v.get('status')=='verified' and v['run'] not in claimed and state['jobs'].get(v['run'],{}).get('phase')!='complete']
                receipts.sort(key=lambda r:r['host']!=host['host'])
                if receipts:
                    receipt=receipts[0];run=receipt['run'];claimed.add(run)
                    state['jobs'][run]={'phase':'running','host':host['host'],'started':stamp()};atomic_json(state_path,state)
                elif expected and all(state['jobs'].get(run,{}).get('phase')=='complete' for run in expected):return
                else:receipt=None
            if receipt is None:time.sleep(5);continue
            try:
                local=copy_receipt(by_host[receipt['host']],host,receipt)
                result=map_watson(host,local,reference,worker_image,report)
                with guard:state['jobs'][run]={'phase':'complete','receipt':result,'finished':stamp()}
            except Exception as error:
                with guard:state['jobs'][run]={'phase':'waiting_or_failed','host':host['host'],'error':str(error)[:500],'updated':stamp()}
                time.sleep(30)
            finally:
                with guard:claimed.discard(run);atomic_json(state_path,state)
    with ThreadPoolExecutor(max_workers=len(strong)) as pool:list(pool.map(worker,strong))
    atomic_json(report/'driver.json',{'phase':'all_watson_runs_aligned','finished':stamp(),'scope':'Not yet merged, called, or published'})


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True)
    watch(parser.parse_args().root)
