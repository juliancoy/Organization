#!/usr/bin/env python3
"""Private reference-match analysis using enrolled, resource-leased swarm workers.

Coordinates remain in owner-only local intermediates and RAM-only worker
containers. Only aggregate match counts belong in public chart snapshots.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

from scripts import genomics_distributed as distributed
from scripts.genomics import private_run
from scripts.genome_reference_compute import finalize

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'swarm-control/server'))


def save(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2));tmp.chmod(0o600);tmp.replace(path)


def identity(path):
    digest=hashlib.sha256()
    before=path.stat()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):digest.update(block)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('Input changed during hashing')
    return {'filename':path.name,'bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'sha256':digest.hexdigest()}


def aggregate_giab(parts):
    summaries={}
    for part in parts:
        name=part['dataset']
        target=summaries.setdefault(name,{'same':0,'benchmark_positions':0,'excluded_variant_span_positions':0,'records':0,'variant_records':0,'prepared_records':0,'chromosomes':{},'workers':set(),'batches':0,'scope':part['scope']})
        if set(target['chromosomes']) & set(part['chromosomes']):raise ValueError('Repeated chromosome in GIAB results')
        target['chromosomes'].update(part['chromosomes']);target['workers'].add(part['host']);target['batches']+=1
        for key in ('same','benchmark_positions','excluded_variant_span_positions','records','variant_records','prepared_records'):target[key]+=part.get(key,0)
    for target in summaries.values():
        target['workers']=sorted(target['workers'])
        if target['same']+target['excluded_variant_span_positions']!=target['benchmark_positions']:raise ValueError('GIAB totals do not partition benchmark')
    return summaries


def julian_source(explicit,folder):
    if explicit is not None:return explicit.resolve(strict=True)
    state_path=folder/'julian/state.json'
    if state_path.exists():
        source=json.loads(state_path.read_text()).get('sources',{}).get('Julian.qc',{}).get('path')
        if source:return Path(source).resolve(strict=True)
    raise ValueError('Provide --julian with the private gVCF source for a new Julian analysis')


def julian(hosts,folder,source,slots):
    distributed.WORKER=(ROOT/'scripts/genome_reference_compute.py').read_text()
    target=folder/'julian';target.mkdir(mode=0o700,exist_ok=True)
    resume=(target/'state.json').exists()
    run=distributed.Run(hosts,target,slots=slots,resume=resume)
    run.save();run.run({'Julian.qc':source.resolve(strict=True)},50000)
    result=finalize(run.results)
    result['workers']=sorted({r['host'] for r in run.results})
    result['batches']=len(run.results)
    result['source']=identity(source)
    save(folder/'Julian-reference-summary.json',result)
    return result


def giab_batches(dataset,bed,vcf,folder,prepared_vcf=None):
    """Create owner-only chromosome inputs; remote inputs stay RAM-only."""
    before={path:(path.stat().st_size,path.stat().st_mtime_ns) for path in (bed,vcf,*([prepared_vcf] if prepared_vcf else []))}
    paths={};counts={};prepared_counts={};streams={}
    try:
        with gzip.open(bed,'rt') if str(bed).endswith('.gz') else open(bed) as rows:
            for line in rows:
                if line.startswith(('#','track','browser')) or not line.strip():continue
                fields=line.split();chrom=fields[0].removeprefix('chr')
                if chrom not in {str(i) for i in range(1,23)}:continue
                if chrom not in streams:
                    path=folder/f'{dataset}-chr{chrom}.input.gz';paths[chrom]=path;counts[chrom]=0;prepared_counts[chrom]=0
                    streams[chrom]=io.TextIOWrapper(gzip.GzipFile(filename=path,mode='wb',compresslevel=1,mtime=0));path.chmod(0o600)
                streams[chrom].write('B\t'+chrom+'\t'+fields[1]+'\t'+fields[2]+'\n')
        for tag,source in [('V',vcf),*([('P',prepared_vcf)] if prepared_vcf else [])]:
            with gzip.open(source,'rt') as rows:
                for line in rows:
                    if line.startswith('#'):continue
                    chrom=line.split('\t',1)[0].removeprefix('chr')
                    if chrom in streams:
                        streams[chrom].write(tag+'\t'+line)
                        if tag=='V':counts[chrom]+=1
                        else:prepared_counts[chrom]+=1
    finally:
        for stream in streams.values():stream.close()
    if any((path.stat().st_size,path.stat().st_mtime_ns)!=stat for path,stat in before.items()):raise RuntimeError('Benchmark input changed during preparation')
    for chrom in sorted(paths,key=int):
        payload=paths[chrom].read_bytes()
        if len(payload)>distributed.MAX_PAYLOAD:raise ValueError('Chromosome exceeds RAM input bound')
        yield {'id':f'{dataset}-chr{chrom}','kind':'giab','dataset':dataset,'records':counts[chrom],'prepared_records':prepared_counts[chrom],
               'payload':payload,'sha256':hashlib.sha256(payload).hexdigest()}


def giab(hosts,folder,sources):
    distributed.WORKER=(ROOT/'scripts/genome_reference_giab.py').read_text()
    target=folder/'giab';target.mkdir(mode=0o700,exist_ok=True)
    jobs=queue.Queue(maxsize=max(2,len(hosts)*2));results=[];errors=[];lock=threading.Lock()
    worker_digest=hashlib.sha256(distributed.WORKER.encode()).hexdigest()
    previous=json.loads((target/'state.json').read_text()) if (target/'state.json').exists() else {}
    if previous and previous.get('worker_source_sha256')!=worker_digest:raise ValueError('Resume requires matching GIAB scientific worker code')
    state={'status':'running','worker_source_sha256':worker_digest,'jobs':{}}
    save(target/'state.json',state)
    def worker(host):
        while True:
            batch=jobs.get()
            try:
                if batch is None:return
                if distributed.STOP.is_set():raise InterruptedError('Reference analysis stopped')
                path=target/(batch['id']+'.json')
                result=json.loads(path.read_text()) if path.exists() else None
                if result is not None and any(result.get(k)!=batch[k] for k in ('kind','records','sha256')):result=None
                if result is None:
                    for attempt in range(3):
                        try:
                            result=distributed.execute(host,batch)
                            if result.get('prepared_records',0)!=batch.get('prepared_records',0):raise ValueError('Prepared row count differs from input')
                            save(path,result);break
                        except distributed.CapacityWait:
                            if attempt==2:raise
                with lock:
                    results.append(result);state['jobs'][batch['id']]={'status':'passed','host':result['host'],'records':result['records']};save(target/'state.json',state)
            except Exception as error:
                with lock:
                    errors.append(type(error).__name__+': '+str(error)[:240]);state['jobs'][batch['id']]={'status':'failed','reason':errors[-1]};save(target/'state.json',state)
            finally:jobs.task_done()
    threads=[threading.Thread(target=worker,args=(host,),daemon=True) for host in hosts if host['eligible']]
    for thread in threads:thread.start()
    try:
        for dataset,source in sources.items():
            for batch in giab_batches(dataset,source['bed'],source['vcf'],target,source.get('prepared_vcf',source.get('prepared'))):
                if distributed.STOP.is_set():raise InterruptedError('Reference analysis stopped')
                with lock:
                    state['jobs'][batch['id']]={'status':'queued','records':batch['records']};save(target/'state.json',state)
                jobs.put(batch)
        jobs.join()
        if errors:raise RuntimeError('GIAB worker jobs failed; inspect private state')
        state['status']='passed';save(target/'state.json',state)
    finally:
        for thread in threads:jobs.put(None)
        for thread in threads:thread.join()
    save(folder/'GIAB-reference-parts.json',results)
    summaries=aggregate_giab(results)
    for name,summary in summaries.items():
        summary['sources']={key:identity(path) for key,path in sources[name].items()}
        provenance=sources[name]['bed'].parent/'reference-benchmark-source.json'
        if provenance.exists():summary['benchmark_source']=json.loads(provenance.read_text())
        save(folder/(name+'-reference-summary.json'),summary)
    save(folder/'GIAB-reference-summary.json',summaries)
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/reference-matches'))
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--hosts',type=Path)
    parser.add_argument('--julian',type=Path,help='Explicit private gVCF source; resume may recover it from private run state')
    parser.add_argument('--slots',type=int,default=2)
    parser.add_argument('--phase',choices=('julian','giab','all'),default='all')
    parser.add_argument('--giab-sources',type=Path,help='Private JSON mapping dataset to bed and vcf paths')
    args=parser.parse_args();os.umask(0o077)
    signal.signal(signal.SIGINT,lambda *_:distributed.STOP.set());signal.signal(signal.SIGTERM,lambda *_:distributed.STOP.set())
    folder=args.resume.resolve(strict=True) if args.resume else private_run(args.output)
    if folder.parent!=args.output.resolve() or folder.is_symlink() or folder.stat().st_mode&0o077:raise ValueError('Private run outside expected output')
    hosts=json.loads(args.hosts.read_text()) if args.hosts else distributed.discover()
    if not any(h['eligible'] for h in hosts):raise RuntimeError('No authenticated accepting swarm workers')
    save(folder/'workers.json',[{k:v for k,v in host.items() if k not in ('ssh','agent_endpoint')} for host in hosts])
    print('Private reference-match run: '+str(folder),flush=True)
    if args.phase in ('julian','all'):
        result=julian(hosts,folder,julian_source(args.julian,folder),args.slots)
        print('Julian reference-call analysis passed across '+str(len(result['workers']))+' workers',flush=True)
    if args.phase in ('giab','all'):
        if not args.giab_sources:raise ValueError('--giab-sources is required for GIAB')
        sources={name:{key:Path(value) for key,value in source.items()} for name,source in json.loads(args.giab_sources.read_text()).items()}
        results=giab(hosts,folder,sources)
        print('GIAB reference-match analysis passed: '+str(len(results))+' chromosome jobs',flush=True)
    print('Reference-match run complete',flush=True)


if __name__=='__main__':main()
