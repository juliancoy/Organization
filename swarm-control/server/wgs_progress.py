"""Owner-only operational WGS progress. Never project reads, genotypes or paths."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import time

ROOT=Path(os.getenv('SWARM_WGS_ROOT','/mnt/PILEDRIVE/genomics-workbench/wgs'))


def read(path):
    try:
        if path.stat().st_size>8*1024*1024:return {}
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def number(value):return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else 0


def tail(path):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-32768));return stream.read().decode(errors='replace')
    except OSError:return ''


def snapshot(root=ROOT):
    downloads=read(root/'downloads/state.json');jobs=downloads.get('jobs',{})
    if not isinstance(jobs,dict):jobs={}
    samples=[]
    for sample in ('Watson','Venter'):
        rows=[row for row in jobs.values() if isinstance(row,dict) and row.get('sample')==sample]
        verified=[row for row in rows if row.get('status')=='verified']
        received=0
        for row in rows:
            progress=row.get('progress') if isinstance(row.get('progress'),dict) else {}
            received+=number(row.get('bytes')) if row.get('status')=='verified' else min(number(row.get('bytes')),max(number(row.get('bytes_completed')),number(progress.get('bytes_completed')),number(progress.get('bytes_received'))))
        samples.append({'sample':sample,'files_total':len(rows),'files_verified':len(verified),'files_active':sum(row.get('status')=='downloading' for row in rows),'files_failed':sum(row.get('status')=='failed' for row in rows),
                        'bytes_total':sum(number(row.get('bytes')) for row in rows),'bytes_verified':sum(number(row.get('bytes')) for row in verified),'bytes_received':received})
    storage=[]
    workers=downloads.get('workers',[])
    for worker in workers if isinstance(workers,list) else []:
        if not isinstance(worker,dict):continue
        host=worker.get('host')
        storage.append({'host':host,'free_bytes_at_start':number(worker.get('free_bytes')),'assigned_bytes':sum(number(row.get('bytes')) for row in jobs.values() if isinstance(row,dict) and row.get('host')==host)})
    index=read(root/'recall/index.json');text=tail(root/'recall/index.stderr')
    lengths=re.findall(r'textLength=(\d+)',text);processed=re.findall(r'(\d+) characters processed',text)
    if not lengths:
        try:lengths=[str(2*sum(int(line.split('\t')[1]) for line in (root/'reference/reference.fa.fai').read_text().splitlines()))]
        except (OSError,ValueError,IndexError):pass
    index_progress={'status':index.get('phase','queued'),'characters_processed':int(processed[-1]) if processed else None,'characters_total':int(lengths[-1]) if lengths else None}
    qc=read(root/'qc/state.json');qc_jobs=qc.get('jobs',{})
    if not isinstance(qc_jobs,dict):qc_jobs={}
    qc_rows=[row for row in qc_jobs.values() if isinstance(row,dict)]
    mappings=read(root/'recall/full/state.json');mapping_jobs=mappings.get('jobs',{})
    if not isinstance(mapping_jobs,dict):mapping_jobs={}
    mapping_rows=[row for row in mapping_jobs.values() if isinstance(row,dict)]
    driver=read(root/'recall/full/driver.json')
    gpu_service=read(root/'gpu-qc/service.json')
    replay=read(root/'qc/gpu-replay/state.json');replay_jobs=replay.get('jobs',{})
    replay_rows=[row for row in replay_jobs.values() if isinstance(row,dict)] if isinstance(replay_jobs,dict) else []
    return {'available':bool(downloads),'sampled_at':time.time(),'download_status':downloads.get('status','not_started'),'updated':downloads.get('updated'),
            'samples':samples,'storage':storage,'index':index_progress,
            'qc':{'status':qc.get('status','queued'),'files':len(qc_rows),'reads_processed':sum(number(row.get('reads',row.get('reads_processed'))) for row in qc_rows),'bases_processed':sum(number(row.get('bases',row.get('bases_processed'))) for row in qc_rows),
                  'gpu_batches':sum(number(row.get('gpu_batches')) for row in qc_rows),'gpu_bases':sum(number(row.get('gpu_bases')) for row in qc_rows),'cpu_bases':sum(number(row.get('cpu_bases')) for row in qc_rows)},
            'gpu_qc':{'status':gpu_service.get('status','not_started'),'host':gpu_service.get('host'),'updated':gpu_service.get('updated'),'batches':number(gpu_service.get('batches')),'bases':number(gpu_service.get('bases'))},
            'gpu_backfill':{'status':replay.get('status','not_started'),'files':len(replay_rows),'verified_files':sum(row.get('phase')=='verified' for row in replay_rows),'batches':sum(number(row.get('gpu_batches')) for row in replay_rows),'bases':sum(number(row.get('gpu_bases')) for row in replay_rows),'cpu_crosschecked_batches':sum(number(row.get('gpu_cpu_crosschecked_batches')) for row in replay_rows)},
            'alignment':{'status':driver.get('phase','queued'),'files':len(mapping_rows),'completed':sum(row.get('status',row.get('phase')) in ('passed','complete','verified') for row in mapping_rows)}}
