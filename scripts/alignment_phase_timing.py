"""Derive disjoint mapper wall-time phases from minimap2's own timestamps.

Sorting overlaps mapping. These timings deliberately exclude transfers, GPU QC,
container startup, and unmeasured sort/verification tails.
"""
import re
import json
import statistics
from pathlib import Path


def parse_log(text):
    loaded=re.search(r'\[M::main::([\d.]+)\*[^]]+\] loaded/built the index',text)
    setup=re.search(r'\[M::mm_idx_stat::([\d.]+)\*',text)
    mapped=re.findall(r'\[M::worker_pipeline::([\d.]+)\*[^]]+\] mapped',text)
    end=re.search(r'Real time: ([\d.]+) sec',text)
    phase='Load reference index' if not loaded else 'Prepare reference index' if not mapped else 'Map reads'
    if end:phase='Sort and verify output'
    result={'current_phase':phase,'scope':'Mapper wall time only; sorting overlaps mapping. Transfers, quality checks, and final verification are excluded.'}
    if loaded and setup and mapped and end:
        boundaries=[0,float(setup[1]),float(mapped[-1]),float(end[1])]
        if boundaries==sorted(boundaries):
            result['seconds']={'reference_setup':boundaries[1], 'mapping_pipeline':boundaries[2]-boundaries[1], 'mapper_cleanup':boundaries[3]-boundaries[2]}
            result['total_seconds']=boundaries[-1]
    return result


def summarize(root,jobs):
    phases=[];current={}
    for job in sorted(jobs,key=lambda j:j.get('finished') or ''):
        host=job.get('host')
        if not host or not job.get('id'):continue
        paths=[Path(root)/'attempts'/(name+'-'+job['id']+'.stderr') for name in (host.replace('.','_'),host)]
        path=next((p for p in paths if p.exists()),None)
        if not path:continue
        try:row=parse_log(path.read_text(errors='replace'))
        except OSError:continue
        if job.get('phase')=='running':
            try:report=json.loads(path.with_suffix('.json').read_text())
            except (OSError,ValueError):report={}
            current[host]='Collect verified output' if report.get('phase')=='complete' else row['current_phase']
        if job.get('phase')=='complete' and row.get('seconds'):phases.append(row)
    if not phases:return {'sample_count':0,'current':current,'scope':'Awaiting completed mapper logs; other phase timings unavailable.'}
    medians={name:statistics.median(row['seconds'][name] for row in phases[-30:]) for name in phases[0]['seconds']}
    total=sum(medians.values());dominant=max(medians,key=medians.get)
    return {'sample_count':min(30,len(phases)),'current':current,'median_seconds':medians,
            'dominant_phase':dominant,'dominant_percent':round(100*medians[dominant]/total,1) if total else 0,
            'scope':phases[-1]['scope'],'basis':'Median of the most recent 30 completed minimap2 batches with complete phase logs; other aligners excluded.'}
