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
            receipt=current['receipt'];source_file=origin+'/'+item['filename']
            # If assembler removed an unmirrored range, fall back to complete,
            # archive-verified gzip; in-flight readers preserve their offset.
            total=(item['bytes']+128*1024*1024-1)//(128*1024*1024)
            if len(copied)<total:
                transfer={**receipt,'path':source_file,'host':current_source['host']};local=copy_receipt(current_source,target,transfer)
                move="import os,pathlib,sys;os.replace(sys.argv[1],sys.argv[2])"
                subprocess.run(distributed.shell_command(target,['python3','-c',move,local['path'],destination+'/'+item['filename']]),check=True)
            else:
                # Reader's compressed stream MD5 is enough to verify its own
                # complete mirrored chunks. A small marker verifies source EOF.
                install(target,destination+'/source-complete.json',json.dumps(receipt).encode());return
            code="import json,pathlib,sys;p=pathlib.Path(sys.argv[1]);r=json.loads(sys.argv[2]);r['mtime_ns']=(p/r['filename']).stat().st_mtime_ns;(p/'receipt.json').write_text(json.dumps(r))"
            subprocess.run(distributed.shell_command(target,['python3','-c',code,destination,json.dumps(receipt)]),check=True);return
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


def map_pair(host,owner,receipt,reference,image,folder):
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
    return {'host':host['host'],'run':run,'bam':work+'/alignment.bam','source_sha256':[r['sha256'] for r in copies],'reference':reference,'image':image,'provisional':True,'generation':receipt['generation']}


def main(root):
    root=Path(root);folder=root/'recall/venter';folder.mkdir(mode=0o700,parents=True,exist_ok=True);full=folder/'full';full.mkdir(mode=0o700,exist_ok=True)
    hosts=json.loads((root/'recall-hosts.json').read_text());by_name={h['host']:h for h in hosts}
    owner=by_name['julian-latitude.local']
    items=sorted([i for i in json.loads((root/'catalog.json').read_text())['files'] if i['sample']=='Venter'],key=lambda i:i['filename'])
    if len(items)!=2:raise RuntimeError('Exactly two Venter mate sources required')
    generation=uuid.uuid4().hex[:12]
    generation_folder=folder/'generations'/generation;generation_folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    full=generation_folder/'full';full.mkdir(mode=0o700,exist_ok=True)
    # Each process generation gets unique shard and BAM paths. Failed source
    # generations can never satisfy a later generation's finalization gate.
    work=str(Path(owner['cache']).parent/'wgs-venter-stream/generations'/generation);input_base=str(Path(owner['cache']).parent)
    atomic_json(folder/'generation.json',{'generation':generation,'phase':'provisional','work':work,'started':stamp(),'publication':False})
    # Mirror beneath the common parent mount, never read partial network writes.
    mirror_base=str(Path(owner['cache']).parent/'wgs-venter-mirror');stop=threading.Event()
    def mirror_forever():
        while not stop.is_set():
            try:mirror(lambda:download_source(root,by_name,items[0]),owner,items[0],mirror_base,stop);return
            except Exception as error:
                atomic_json(folder/'mirror.json',{'phase':'retrying','error':str(error)[:500],'updated':stamp()});stop.wait(2)
    thread=threading.Thread(target=mirror_forever,daemon=True);thread.start()
    subprocess.run(distributed.shell_command(owner,['mkdir','-p','--',work+'/tmp',mirror_base,str(Path(owner['cache'])/file_id(items[1]))]),check=True)
    job={'items':items,'folders':{items[0]['filename']:'/mirror/'+file_id(items[0]),items[1]['filename']:'/mate2'}}
    install(owner,work+'/job.json',json.dumps(job).encode());install(owner,work+'/stream_worker.py',streaming_worker_code().encode())
    stream_errors=[]
    def stream():
        try:
            while True:
                try:
                    available=capacity(owner,1024,1)
                    leased_run(owner,owner['image'],'python /work/stream_worker.py',[(mirror_base,'/mirror',True),(str(Path(owner['cache'])/file_id(items[1])),'/mate2',True),(work,'/work',False)],work,1,1024,folder/'stream.json');return
                except distributed.CapacityWait:time.sleep(5)
        except Exception as error:
            atomic_json(folder/'generation.json',{'generation':generation,'phase':'invalid','error':str(error)[:500],'work':work,'publication':False,'updated':stamp()})
            stream_errors.append(str(error))
    threading.Thread(target=stream,daemon=True).start()
    state_path=folder/'full/state.json';state={'jobs':{},'generation':generation}
    atomic_json(state_path,state)
    atomic_json(folder/'full/driver.json',{'phase':'streaming_pair_shards_and_waiting_reference_index','started':stamp(),'provisional':True,'generation':generation})
    while not (root/'reference/attestation.json').exists():
        if stream_errors:raise RuntimeError(stream_errors[0])
        time.sleep(5)
    guard=threading.Lock();claimed=set();mapping_hosts=[h for h in hosts if h['host'] in ('compai.local','codecollective.local','julian-latitude.local','julian-inspiron-3793.local')]
    listing="import json,pathlib,sys;p=pathlib.Path(sys.argv[1]);print(json.dumps({'shards':[json.loads(f.read_text()) for f in sorted(p.glob('[0-9]*.json'))],'verified':json.loads((p/'stream-verified.json').read_text()) if (p/'stream-verified.json').exists() else None}))"
    def mapper(host):
        from docker_utils.images import ImageResolver
        resolver=ImageResolver([h for h in hosts if not h['local']],lambda h:h['ssh']);resolver.ensure(IMAGE,[] if host['local'] else [host]);image=resolver.inspect(None if host['local'] else host,IMAGE)
        reference=distribute_reference(host,root/'reference')
        while True:
            if stream_errors:raise RuntimeError(stream_errors[0])
            data=json.loads(subprocess.check_output(distributed.shell_command(owner,['python3','-c',listing,work+'/shards']),text=True))
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
    while True:
        downloads=json.loads((root/'downloads/state.json').read_text())['jobs']
        if all(downloads.get(file_id(i),{}).get('status')=='verified' for i in items):break
        time.sleep(5)
    atomic_json(folder/'generation.json',{'generation':generation,'phase':'verified','work':work,'publication':False,'updated':stamp()})
    atomic_json(folder/'full/driver.json',{'phase':'all_venter_shards_aligned_and_sources_verified','finished':stamp(),'provisional':False,'generation':generation,'shards':len(state['jobs']),'publication':False})

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);main(p.parse_args().root)
