#!/usr/bin/env python3
"""Stream paired Venter reads from complete download chunks into swarm mapping.

Source mirrors copy completed .bin/.json ranges only. Pair-shard mappings remain
provisional until both complete original FASTQs pass publisher MD5 and QC.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid
from scripts import genomics_distributed as distributed
from scripts.wgs_swarm_download import atomic_json,file_id,stamp
from scripts.wgs_recall_swarm import AgentClient,leased_run,copy_receipt,distribute_reference,GROUP,IMAGE
from scripts.wgs_finalize import install,remote_json,capacity
from scripts.wgs_recall import alignment


def download_source(root, hosts, item):
    """Resolve the current enrolled download owner and its current cache root."""
    state=json.loads((Path(root)/'downloads/state.json').read_text())
    row=state.get('jobs',{}).get(file_id(item),{})
    name=row.get('host')
    if name not in hosts:raise RuntimeError('Download owner is absent or not enrolled')
    worker=next((w for w in state.get('workers',[]) if w['host']==name),{})
    return {**hosts[name],'cache':worker.get('cache',hosts[name]['cache'])}


def mirror(source,target,item,folder,stop):
    """Continuously mirror only committed chunks; use verified final fallback."""
    ident=file_id(item);destination=folder+'/'+ident
    subprocess.run(distributed.shell_command(target,['mkdir','-p','--',destination+'/chunks']),check=True)
    listing="""import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);r=p/'receipt.json';out={'chunks':[]}
if r.exists():out['receipt']=json.loads(r.read_text())
for q in (p/'chunks').glob('*.json'):
 try:
  m=json.loads(q.read_text());b=q.with_suffix('.bin')
  if b.exists() and b.stat().st_size==m['bytes']:out['chunks'].append(int(q.stem))
 except FileNotFoundError:pass
print(json.dumps(out))
"""
    try:copied=set(json.loads(subprocess.check_output(distributed.shell_command(target,['python3','-c',listing,destination]),text=True))['chunks'])
    except Exception:copied=set()
    while not stop.is_set():
        current_source=source() if callable(source) else source
        origin=str(Path(current_source['cache'])/ident)
        result=subprocess.run(distributed.shell_command(current_source,['python3','-c',listing,origin]),capture_output=True,text=True,check=True)
        current=json.loads(result.stdout)
        for index in sorted(set(current['chunks'])-copied):
            for suffix in ('.bin','.json'):
                # copy_receipt enforces SHA before atomic publication. Metadata is
                # copied second, so OrderedChunks never observes half a chunk.
                path=origin+'/chunks/'+str(index)+suffix
                from scripts.wgs_finalize import attest
                receipt=attest(current_source,path);incoming=copy_receipt(current_source,target,receipt)
                move="import os,pathlib,sys;a,b=map(pathlib.Path,sys.argv[1:]);b.parent.mkdir(mode=0o700,parents=True,exist_ok=True);os.replace(a,b)"
                subprocess.run(distributed.shell_command(target,['python3','-c',move,incoming['path'],destination+'/chunks/'+str(index)+suffix]),check=True)
            copied.add(index)
        if 'receipt' in current:
            # A prior chunk mirror can predate a repaired publisher source.
            # Never bless it merely because all chunk numbers are present.
            receipt=current['receipt'];source_file=origin+'/'+item['filename']
            transfer={**receipt,'path':source_file,'host':current_source['host']}
            local=copy_receipt(current_source,target,transfer,resume=True)
            publish="""import json,os,pathlib,sys
source=pathlib.Path(sys.argv[1]);folder=pathlib.Path(sys.argv[2]);r=json.loads(sys.argv[3]);final=folder/r['filename']
temp=final.with_suffix(final.suffix+'.verified');temp.unlink(missing_ok=True);os.link(source,temp);os.replace(temp,final)
r['mtime_ns']=final.stat().st_mtime_ns
p=folder/'receipt.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(r));tmp.chmod(0o600);os.replace(tmp,p)
"""
            subprocess.run(distributed.shell_command(target,['python3','-c',publish,local['path'],destination,json.dumps(receipt)]),check=True)
            return
        stop.wait(2)


def streaming_worker_code():
    qc=(Path(__file__).parent/'wgs_stream_qc.py').read_text().split('def qc_memory_mb(')[0]
    paired=(Path(__file__).parent/'wgs_paired_shards.py').read_text().split("if __name__")[0]
    return qc+'\n'+paired+r'''
import contextlib,pathlib
job=json.loads(pathlib.Path('/work/job.json').read_text());readers=[]
with contextlib.ExitStack() as stack:
 streams=[]
 for item in job['items']:
  reader=OrderedChunks(job['folders'][item['filename']],item);readers.append(reader)
  raw=stack.enter_context(io.BufferedReader(reader,buffer_size=1024*1024))
  streams.append(stack.enter_context(gzip.GzipFile(fileobj=raw,mode='rb')))
 result=split_opened(streams[0],streams[1],'/work/shards',500000)
 for reader,item in zip(readers,job['items']):
  if reader.offset!=item['bytes'] or reader.md5.hexdigest()!=item['md5']:raise RuntimeError('Paired stream failed full original publisher MD5')
 result.update(phase='paired_stream_md5_verified',publication=False,source_md5=[i['md5'] for i in job['items']])
 pathlib.Path('/work/shards/stream-verified.json').write_text(json.dumps(result))
'''


def mapping_complete(jobs,verified,generation):
    if not isinstance(verified,dict) or type(verified.get('shards')) is not int or verified['shards']<1:
        return False
    expected={'SRR7097858-'+generation+'-'+str(n).zfill(6) for n in range(verified['shards'])}
    return set(jobs)==expected and all(j.get('phase')=='complete' and j.get('receipt',{}).get('generation')==generation for j in jobs.values())


def map_pair(host,owner,receipt,reference,image,folder):
    available=capacity(host,7000,2)
    copies=[]
    for item in receipt['files']:
        item={**item,'host':owner['host']};copies.append(copy_receipt(owner,host,item))
    available=capacity(host,7000,2);cpus=min(host['cpus']-1,available['available_cpus']);memory=max(7000,available['available_mb']//2)
    run='SRR7097858-'+receipt['generation']+'-'+str(receipt['shard']).zfill(6);work=str(Path(host['cache']).parent/'wgs-alignments'/run)
    subprocess.run(distributed.shell_command(host,['mkdir','-p','--',work+'/tmp']),check=True)
    mounts=[(reference,'/ref',True),(work,'/work',False)]+[(r['path'],'/mate'+str(n)+'.fastq.gz',True) for n,r in enumerate(copies,1)]
    command='rm -f /work/alignment.partial.bam /work/alignment.partial.bam.bai && '+alignment('/ref/reference.fa',['/mate1.fastq.gz','/mate2.fastq.gz'],'/work/alignment.partial.bam','Venter',run,cpus,memory)
    command+=' && samtools quickcheck /work/alignment.partial.bam && samtools index /work/alignment.partial.bam && samtools flagstat -O json /work/alignment.partial.bam > /work/flagstat.json && mv /work/alignment.partial.bam /work/alignment.bam && mv /work/alignment.partial.bam.bai /work/alignment.bam.bai'
    leased_run(host,image,command,mounts,work,cpus,memory,folder/(run+'.json'))
    result={'host':host['host'],'run':run,'bam':work+'/alignment.bam','source_sha256':[r['sha256'] for r in copies],'reference':reference,'image':image,'provisional':True,'generation':receipt['generation']}
    atomic_json(folder/(run+'.receipt.json'),result)
    return result


class NeedsAttention(RuntimeError):
    """Permanent stream failure; retain artifacts and require investigation."""


def source_fingerprint(items):
    return hashlib.sha256(json.dumps([{k:i[k] for k in ('filename','bytes','md5')} for i in items],sort_keys=True).encode()).hexdigest()


def reusable_generation(previous, items):
    if not previous or previous.get('source_fingerprint')!=source_fingerprint(items):return False
    if previous.get('phase') in ('invalid','needs_attention'):
        raise NeedsAttention('Venter generation needs investigation; preserved evidence and shards, refusing a restart loop')
    return previous.get('phase') in ('provisional','verified') and bool(previous.get('generation'))


def transient_stream_failure(report):
    # Parsing, pairing and checksum failures are never silently retried.
    return report.get('exit_code') in (137,143,255) or bool(report.get('lease_errors'))


def main(root):
    root=Path(root);folder=root/'recall/venter';folder.mkdir(mode=0o700,parents=True,exist_ok=True);full=folder/'full';full.mkdir(mode=0o700,exist_ok=True)
    hosts=json.loads((root/'recall-hosts.json').read_text());by_name={h['host']:h for h in hosts}
    owner=by_name['julian-latitude.local']
    items=sorted([i for i in json.loads((root/'catalog.json').read_text())['files'] if i['sample']=='Venter'],key=lambda i:i['filename'])
    if len(items)!=2:raise RuntimeError('Exactly two Venter mate sources required')
    previous=json.loads((folder/'generation.json').read_text()) if (folder/'generation.json').exists() else {}
    generation=previous['generation'] if reusable_generation(previous,items) else uuid.uuid4().hex[:12]
    generation_folder=folder/'generations'/generation;generation_folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    full=generation_folder/'full';full.mkdir(mode=0o700,exist_ok=True)
    # Each process generation gets unique shard and BAM paths. Failed source
    # generations can never satisfy a later generation's finalization gate.
    work=str(Path(owner['cache']).parent/'wgs-venter-stream/generations'/generation);input_base=str(Path(owner['cache']).parent)
    atomic_json(folder/'generation.json',{'generation':generation,'phase':'provisional','work':work,'started':previous.get('started',stamp()) if previous.get('generation')==generation else stamp(),'publication':False,'source_fingerprint':source_fingerprint(items)})
    # Mirror beneath the common parent mount, never read partial network writes.
    mirror_base=str(Path(owner['cache']).parent/'wgs-venter-mirror');stop=threading.Event()
    # Stage both current verified mates. Old mirrored ranges and provisional
    # mappings must not be reused after source repair.
    for item in items:
        atomic_json(folder/'mirror.json',{'phase':'staging_verified_source','filename':item['filename'],'generation':generation,'updated':stamp()})
        source=download_source(root,by_name,item)
        source_state=json.loads((root/'downloads/state.json').read_text())['jobs'][file_id(item)]
        if source_state.get('status')!='verified':raise distributed.CapacityWait('Waiting for publisher-verified source')
        mirror(source,owner,item,mirror_base,stop)
    atomic_json(folder/'mirror.json',{'phase':'both_verified_sources_staged','generation':generation,'updated':stamp()})
    subprocess.run(distributed.shell_command(owner,['mkdir','-p','--',work+'/tmp']),check=True)
    job={'items':items,'folders':{item['filename']:'/mirror/'+file_id(item) for item in items}}
    install(owner,work+'/job.json',json.dumps(job).encode());install(owner,work+'/stream_worker.py',streaming_worker_code().encode())
    stream_errors=[]
    def stream():
        failures=0
        while not stop.is_set():
            try:
                capacity(owner,1024,1)
                leased_run(owner,owner['image'],'python /work/stream_worker.py',[(mirror_base,'/mirror',True),(work,'/work',False)],work,1,1024,folder/'stream.json')
                return
            except distributed.CapacityWait:
                stop.wait(5)
            except Exception as error:
                failures+=1
                report=json.loads((folder/'stream.json').read_text()) if (folder/'stream.json').exists() else {}
                retry=transient_stream_failure(report) and failures<4
                atomic_json(folder/'generation.json',{'generation':generation,'phase':'provisional' if retry else 'needs_attention','error':str(error)[:1000],'work':work,'publication':False,'source_fingerprint':source_fingerprint(items),'updated':stamp(),'stream_failures':failures})
                if retry:
                    stop.wait(min(60,5*3**(failures-1)))
                    continue
                stream_errors.append(str(error))
                return
    threading.Thread(target=stream,daemon=True).start()
    state_path=folder/'full/state.json'
    saved=json.loads(state_path.read_text()) if state_path.exists() else {}
    state={'jobs':{key:job for key,job in saved.get('jobs',{}).items() if saved.get('generation')==generation and job.get('phase')=='complete' and job.get('receipt',{}).get('generation')==generation},'generation':generation}
    # Mapping receipts are committed independently before the shared state file.
    for receipt_path in full.glob('*.receipt.json'):
        receipt=json.loads(receipt_path.read_text())
        if receipt.get('generation')==generation and receipt.get('bam') and receipt.get('source_sha256'):
            state['jobs'][receipt_path.name.removesuffix('.receipt.json')]={'phase':'complete','receipt':receipt}
    atomic_json(state_path,state)
    atomic_json(folder/'full/driver.json',{'phase':'streaming_pair_shards_and_waiting_reference_index','started':stamp(),'provisional':True,'generation':generation})
    while not (root/'reference/attestation.json').exists():
        if stream_errors:raise NeedsAttention(stream_errors[0])
        time.sleep(5)
    guard=threading.Lock();claimed=set();mapping_hosts=[h for h in hosts if h['host'] in ('compai.local','codecollective.local','julian-latitude.local','julian-inspiron-3793.local')]
    listing="import json,pathlib,sys;p=pathlib.Path(sys.argv[1]);print(json.dumps({'shards':[json.loads(f.read_text()) for f in sorted(p.glob('[0-9]*.json'))],'verified':json.loads((p/'stream-verified.json').read_text()) if (p/'stream-verified.json').exists() else None}))"
    def mapper(host):
        from docker_utils.images import ImageResolver
        try:
            resolver=ImageResolver([h for h in hosts if not h['local']],lambda h:h['ssh']);resolver.ensure(IMAGE,[] if host['local'] else [host]);image=resolver.inspect(None if host['local'] else host,IMAGE)
            reference=distribute_reference(host,root/'reference')
        except (OSError,RuntimeError,subprocess.SubprocessError) as error:
            # An optional worker must not abort healthy mapping workers.
            with guard:
                state.setdefault('workers',{})[host['host']]={'phase':'unavailable','error':str(error)[:500]};atomic_json(state_path,state)
            return
        while True:
            if stream_errors:raise NeedsAttention(stream_errors[0])
            try:
                data=json.loads(subprocess.check_output(distributed.shell_command(owner,['python3','-c',listing,work+'/shards']),text=True,timeout=30))
            except (OSError,ValueError,subprocess.SubprocessError) as error:
                with guard:
                    state.setdefault('workers',{})[host['host']]={'phase':'waiting_for_shards','error':str(error)[:500]};atomic_json(state_path,state)
                time.sleep(5);continue
            with guard:
                candidates=[r for r in data['shards'] if r['shard'] not in claimed and state['jobs'].get('SRR7097858-'+generation+'-'+str(r['shard']).zfill(6),{}).get('phase')!='complete']
                if candidates:r=candidates[0];claimed.add(r['shard'])
                elif data['verified'] and len(state['jobs'])==data['verified']['shards'] and all(j['phase']=='complete' for j in state['jobs'].values()):return
                else:r=None
            if r is None:time.sleep(5);continue
            run='SRR7097858-'+generation+'-'+str(r['shard']).zfill(6)
            # Rewrite container-local shard paths to absolute owner filesystem.
            r={**r,'generation':generation,'files':[{**f,'path':work+'/shards/'+Path(f['path']).name} for f in r['files']]}
            try:
                result=map_pair(host,owner,r,reference,image,full)
                with guard:state['jobs'][run]={'phase':'complete','receipt':result,'finished':stamp()}
            except Exception as error:
                with guard:state['jobs'][run]={'phase':'waiting_or_failed','host':host['host'],'error':str(error)[:500],'updated':stamp()}
                time.sleep(30)
            finally:
                with guard:claimed.discard(r['shard']);atomic_json(state_path,state)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(mapper,mapping_hosts))
    final_data=json.loads(subprocess.check_output(distributed.shell_command(owner,['python3','-c',listing,work+'/shards']),text=True))
    if not mapping_complete(state['jobs'],final_data.get('verified'),generation):
        raise RuntimeError('Not every current-generation paired shard has a verified mapping')
    while True:
        downloads=json.loads((root/'downloads/state.json').read_text())['jobs']
        if all(downloads.get(file_id(i),{}).get('status')=='verified' for i in items):break
        time.sleep(5)
    atomic_json(folder/'generation.json',{'generation':generation,'phase':'verified','work':work,'publication':False,'source_fingerprint':source_fingerprint(items),'updated':stamp()})
    atomic_json(folder/'full/driver.json',{'phase':'all_venter_shards_aligned_and_sources_verified','finished':stamp(),'provisional':False,'generation':generation,'shards':len(state['jobs']),'publication':False})

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True)
    try:main(p.parse_args().root)
    except NeedsAttention as error:
        import sys
        print(str(error),file=sys.stderr)
        raise SystemExit(78)
