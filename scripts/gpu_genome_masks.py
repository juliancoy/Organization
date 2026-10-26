#!/usr/bin/env python3
"""Private tiled reference-status mask prototype. Never publishes chart data.

REF spans exclude reference equality. Only an independently confident variant
START can establish non-reference status. Agreement compares reference status,
never alternate alleles or genotypes. Absent evidence always remains unknown.
"""
import argparse
from array import array
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import os
import socket
import time
import uuid
from scripts.genomics import private_run,write_json

PRIMARY={str(i) for i in range(1,23)}|{'X','Y','MT'}
FLAGS={'confirmed_reference':1,'variant_ref_spans':2,'unknown':4,'confident_variant_starts':8}
MAX_TILE=1048576
METRICS=69


def normalize(value):
    chrom=str(value).removeprefix('chr');return 'MT' if chrom=='M' else chrom


def validate(manifest):
    if manifest.get('assembly')!='GRCh38':raise ValueError('Only GRCh38 is accepted; do not mix assemblies')
    chrom=normalize(manifest['chromosome'])
    if chrom not in PRIMARY:raise ValueError('Only canonical chromosomes are accepted')
    start,end,length=manifest.get('start',0),manifest['end'],manifest['chromosome_length']
    if any(type(v) is not int for v in (start,end,length)) or not 0<=start<end<=length:raise ValueError('Invalid canonical region')
    samples=manifest['samples']
    if len(samples)!=6 or len({s['id'].casefold() for s in samples})!=6:raise ValueError('Exactly six distinct sample IDs required')
    if manifest.get('source_scope') not in ('synthetic_known_fixture','curated_confidence_evidence'):raise ValueError('Declare synthetic fixture or curated confidence evidence')
    if 'reference_N' not in manifest:raise ValueError('Explicit reference N mask is required, including when empty')
    intervals=[]
    for sample in samples:
        for key in FLAGS:intervals.extend(sample.get(key,[]))
    intervals.extend(manifest['reference_N'])
    if len(intervals)>100000:raise ValueError('Prototype manifest interval bound exceeded; split canonical regions')
    for item in intervals:
        if len(item)!=2 or any(type(v) is not int for v in item) or not 0<=item[0]<item[1]<=length:raise ValueError('Invalid zero-based half-open mask interval')
    return chrom,start,end


def segments(manifest):
    chrom,start,end=validate(manifest);events=defaultdict(list)
    def add(rows,bit):
        for a,b in rows:
            a=max(a,start);b=min(b,end)
            if a<b:events[a].append((bit,1));events[b].append((bit,-1))
    for index,sample in enumerate(manifest['samples']):
        for key,flag in FLAGS.items():add(sample.get(key,[]),flag<<(index*4))
    add(manifest['reference_N'],1<<31);events[start];events[end]
    active=defaultdict(int);coordinates=sorted(events)
    watson=next((i for i,s in enumerate(manifest['samples']) if s['id'].casefold()=='watson'),None)
    for pos,following in zip(coordinates,coordinates[1:]):
        for bit,change in events[pos]:active[bit]+=change
        word=0
        for bit,count in active.items():
            if count>0:word|=bit
        if chrom=='19' and watson is not None:
            word=(word&~(15<<(watson*4)))|(4<<(watson*4))
        if following>pos:yield pos,following,word


def word_counts(word):
    result=[0]*METRICS;states=[]
    for person in range(6):
        bits=(word>>(person*4))&15;ref=bool(bits&1);span=bool(bits&2);variant_start=bool(bits&8)
        if span:result[18+person]+=1
        unknown=bool(word&(1<<31) or bits&4 or ref and span)
        state=0 if unknown else 2 if span and variant_start else 1 if ref and not span and not variant_start else 0
        states.append(state);result[(0 if state==1 else 6 if state==2 else 12)+person]+=1
    pair=0
    for left in range(6):
        for right in range(left+1,6):
            if states[left] and states[right]:result[24+pair*3]+=1;result[25+pair*3+(states[left]!=states[right])]+=1
            pair+=1
    return result


def oracle_words(words):
    result=[0]*METRICS
    for word in words:
        for index,value in enumerate(word_counts(word)):result[index]+=value
    return result


def oracle(manifest):
    result=[0]*METRICS
    for start,end,word in segments(manifest):
        for index,value in enumerate(word_counts(word)):result[index]+=value*(end-start)
    return result


def tiles(manifest,tile_positions=MAX_TILE):
    if not 1<=tile_positions<=MAX_TILE:raise ValueError('Invalid bounded tile size')
    if sys.byteorder!='little':raise RuntimeError('Packed mask protocol requires little-endian host')
    tile=array('I')
    for start,end,word in segments(manifest):
        left=end-start
        while left:
            amount=min(left,tile_positions-len(tile));tile.extend(array('I',[word])*amount);left-=amount
            if len(tile)==tile_positions:yield tile;tile=array('I')
    if tile:yield tile


def decode(manifest,counts):
    if len(counts)!=METRICS or any(type(v) is not int or v<0 for v in counts):raise ValueError('Invalid counter vector')
    _,start,end=validate(manifest);people=[];pairs=[]
    for i,sample in enumerate(manifest['samples']):
        if sum(counts[j+i] for j in (0,6,12))!=end-start:raise ValueError('Categories do not partition the canonical region')
        people.append({'id':sample['id'],'confirmed_reference':counts[i],'confident_variant_site_starts':counts[6+i],'unknown':counts[12+i],'variant_span_covered':counts[18+i]})
    index=0
    for left in range(6):
        for right in range(left+1,6):
            known,agree,differ=counts[24+index*3:27+index*3]
            if agree+differ!=known or known>end-start:raise ValueError('Invalid jointly known pair counts')
            pairs.append({'left':people[left]['id'],'right':people[right]['id'],'jointly_known':known,'reference_status_agree':agree,'reference_status_differ':differ,'agreement_fraction':agree/known if known else None});index+=1
    return {'samples':people,'pairs':pairs,'concordance_kind':'reference status only; not allele or genotype concordance','source_scope':manifest['source_scope'],'chromosome':normalize(manifest['chromosome']),'start':start,'end':end,'publication':False,'prototype':True,'watson_chromosome19':'always unknown','reference_N':'always unknown','variant_span_semantics':'exclusion; deletion interiors never imply reference equality'}


def run(manifest,executable,shader,output,tile_positions=MAX_TILE,pack_only=False):
    folder=private_run(output);expected=oracle(manifest);manifest_bytes=json.dumps(manifest,sort_keys=True).encode()
    write_json(folder/'provenance.json',{'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),'source_scope':manifest['source_scope'],'tile_positions':tile_positions,'publication':False,'model':'reference_status_masks_v1'})
    if pack_only:
        with (folder/'masks.bin').open('xb') as stream:
            for tile in tiles(manifest,tile_positions):stream.write(tile.tobytes())
        write_json(folder/'oracle.json',{'counts':expected,**decode(manifest,expected)});return folder
    with (folder/'stderr.txt').open('xb') as errors:
        process=subprocess.Popen([str(executable),'-',str(shader),str(tile_positions)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors)
        try:
            for tile in tiles(manifest,tile_positions):process.stdin.write(tile.tobytes())
            process.stdin.close();process.stdin=None;stdout,_=process.communicate()
        finally:
            if process.poll() is None:process.kill();process.wait()
    if process.returncode:raise RuntimeError('GPU dispatch failed; inspect private stderr report')
    result=json.loads(stdout)
    if not result.get('exact_match') or result.get('software_fallback') or result.get('device_type') not in (1,2) or result.get('counts')!=expected:raise ValueError('Physical GPU must exactly match the independent CPU oracle')
    write_json(folder/'result.json',{**result,**decode(manifest,result['counts'])});return folder


def run_cpu(manifest,output):
    folder=private_run(output);wall=time.perf_counter();thread=time.thread_time();counts=oracle(manifest)
    result={**decode(manifest,counts),'counts':counts,'backend':'cpu_interval_oracle','cpu_thread_ms':(time.thread_time()-thread)*1000,'wall_ms':(time.perf_counter()-wall)*1000,'production_acceleration_enabled':False}
    write_json(folder/'result.json',result);write_json(folder/'provenance.json',{'manifest_sha256':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),'source_scope':manifest['source_scope'],'publication':False,'model':'reference_status_masks_v1'})
    return folder


def daemon_request(socket_path,request,host=None):
    if host is None:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(120);connection.connect(str(socket_path));connection.sendall(json.dumps(request).encode()+b'\n')
            with connection.makefile('rb') as stream:response=json.loads(stream.readline(1048576))
    else:
        from scripts.genomics_distributed import shell_command
        code="import json,socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(120);s.connect(sys.argv[1]);s.sendall(sys.argv[2].encode()+b'\\n');f=s.makefile('rb');print(f.readline(1048576).decode());s.close()"
        response=json.loads(subprocess.check_output(shell_command(host,['python3','-c',code,str(socket_path),json.dumps(request)]),text=True,timeout=130))
    if response.get('request_id')!=request['request_id'] or response.get('error'):raise ValueError('Daemon rejected tile or response identity changed: '+str(response.get('error')))
    if response.get('backend')!='vulkan' or not response.get('validated_backend') or response.get('software_fallback') is not False or response.get('device_type') not in (1,2):raise ValueError('A validated physical GPU backend is required')
    return response


def run_daemon(manifest,socket_path,batch_dir,output,tile_positions=MAX_TILE,host=None):
    if host is not None:
        from scripts.wgs_swarm_download import AgentClient
        from scripts.genomics_distributed import eligible
        client=AgentClient(host)
        try:
            status=client.call('status',timeout=15)
            if status.get('machine')!=host['machine'] or not eligible(status):raise ValueError('Signed worker identity or admission policy changed')
        finally:client.close()
    folder=private_run(output);t0=time.perf_counter();cpu0=time.thread_time();expected=oracle(manifest);cpu_thread_ms=(time.thread_time()-cpu0)*1000;cpu_wall_ms=(time.perf_counter()-t0)*1000
    aggregate=[0]*METRICS;requests=[];controller_ms=0
    for tile in tiles(manifest,tile_positions):
        ident=uuid.uuid4().hex;path=str(Path(batch_dir)/(ident+'.mask.bin'));payload=tile.tobytes();started=time.perf_counter()
        if host is None:
            parent=Path(batch_dir)
            if parent.is_symlink() or parent.stat().st_uid!=os.getuid():raise ValueError('Unsafe daemon batch directory')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(payload)
        else:
            from scripts.genomics_distributed import shell_command
            writer="import os,pathlib,sys;p=pathlib.Path(sys.argv[1]);parent=p.parent;assert parent.is_dir() and not parent.is_symlink() and parent.stat().st_uid==os.getuid();fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);s=os.fdopen(fd,'wb');s.write(sys.stdin.buffer.read());s.close()"
            subprocess.run(shell_command(host,['python3','-c',writer,path]),input=payload,check=True,timeout=60)
        request={'version':1,'request_id':ident,'op':'mask','mask_path':path,'verify':False}
        try:response=daemon_request(socket_path,request,host)
        finally:
            if host is None:Path(path).unlink(missing_ok=True)
            else:subprocess.run(shell_command(host,['python3','-c','import pathlib,sys;pathlib.Path(sys.argv[1]).unlink(missing_ok=True)',path]),check=True,timeout=30)
        controller_ms+=(time.perf_counter()-started)*1000
        values=response.get('mask_counts')
        if response.get('positions')!=len(tile) or not isinstance(values,list) or len(values)!=METRICS or any(type(v) is not int or v<0 for v in values):raise ValueError('Malformed GPU mask response')
        for j,value in enumerate(values):aggregate[j]+=value
        requests.append({'positions':len(tile),'timings':response.get('timings'),'device':response['device']})
    if aggregate!=expected:raise ValueError('Tiled physical GPU result differs from independent CPU mask oracle')
    result={**decode(manifest,aggregate),'counts':aggregate,'exact_match':True,'device':requests[0]['device'],'backend':'vulkan','tiles':len(requests),'max_tile_positions':tile_positions,'tile_metrics':requests,'cpu_interval_oracle_thread_ms':cpu_thread_ms,'cpu_interval_oracle_wall_ms':cpu_wall_ms,'controller_upload_socket_readback_ms':controller_ms,'timing_scope':'Controller includes tile file upload, serialized daemon queue/request/readback and file cleanup; CPU oracle operates on sparse intervals','production_acceleration_enabled':False}
    write_json(folder/'result.json',result);write_json(folder/'provenance.json',{'manifest_sha256':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),'source_scope':manifest['source_scope'],'publication':False,'model':'reference_status_masks_v1','daemon_context':'shared persistent serialized GPU worker'})
    return folder


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--executable',type=Path);p.add_argument('--shader',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--tile-positions',type=int,default=MAX_TILE);p.add_argument('--pack-only',action='store_true');p.add_argument('--cpu-only',action='store_true');p.add_argument('--socket',type=Path);p.add_argument('--batch-dir',type=Path);p.add_argument('--host');p.add_argument('--hosts',type=Path);args=p.parse_args()
    if args.cpu_only:
        print(run_cpu(json.loads(args.manifest.read_text()),args.output));return
    if args.socket:
        if not args.batch_dir:p.error('--batch-dir required for daemon mode')
        host=None
        if args.host:
            from scripts.genomics_distributed import discover
            hosts=json.loads(args.hosts.read_text()) if args.hosts else discover();host=next(h for h in hosts if h['host']==args.host and h.get('eligible'))
        print(run_daemon(json.loads(args.manifest.read_text()),args.socket,args.batch_dir,args.output,args.tile_positions,host));return
    if not args.pack_only and (args.executable is None or args.shader is None):p.error('GPU executable and shader required')
    print(run(json.loads(args.manifest.read_text()),args.executable,args.shader,args.output,args.tile_positions,args.pack_only))

if __name__=='__main__':main()
