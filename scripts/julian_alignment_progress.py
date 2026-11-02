"""Expose operational alignment progress, never private reads or genetic results."""
import argparse
import datetime
import json
import re
import statistics
from pathlib import Path
import subprocess
import time
from scripts.task_bottlenecks import classify
from scripts.alignment_phase_timing import summarize as phase_summary


def read(path, default=None):
    try: return json.loads(Path(path).read_text())
    except (OSError, ValueError): return default if default is not None else {}


def snapshot(root, service_active=True):
    root = Path(root); state = read(root / 'state.json'); manifest = read(root / 'manifest.json')
    jobs = list(state.get('jobs', {}).values())
    done = [j for j in jobs if j.get('phase') == 'complete']
    active = [j for j in jobs if j.get('phase') == 'running']
    failures = [j for j in jobs if j.get('phase') == 'needs_attention']
    total = sum(f.get('bytes', 0) for f in manifest.get('reads', []))
    completed = sum(sum(b - a for a, b in zip(j['start_offsets'], j['end_offsets'])) for j in done)
    complete = bool(read(root / 'final/complete.json'))
    index = read(root / 'reference-index.json'); bwa = read(root / 'bwa-index.json')
    hosts = sorted(set(j.get('host') for j in active if j.get('host')))
    gpu_hosts = set()
    for path in root.glob('gpu-validation-*.json'):
        row = read(path)
        if row.get('matches_independent_source_cpu_histogram'): gpu_hosts.add(row.get('host'))
    for job in done:
        if job.get('gpu_qc'): gpu_hosts.add(job.get('host'))
    detail = f"{len(done):,} batches / {2 * sum(j.get('pairs', 0) for j in done):,} reads aligned; {len(active)} active batches."
    detail += ' Whole-file progress uses verified aligned input bytes, not just batches discovered so far.'
    if not service_active and not complete: detail += ' Controller stopped; saved progress retained.'
    status = 'completed' if complete else 'failed' if failures else 'running' if service_active else 'interrupted'
    alignment_done = bool(state.get('producer_complete') and jobs and len(done) == len(jobs))
    worker_states = state.get('workers', {})
    reconnecting = [h for h, value in worker_states.items() if value.get('phase') in ('unavailable', 'reconnecting')]
    mapping_workers = [v for v in worker_states.values() if v.get('phase') != 'gpu_quality_checks']
    if service_active and not complete and not alignment_done and not active and mapping_workers and all(
            v.get('phase') in ('unavailable', 'reconnecting') for v in mapping_workers):
        status = 'waiting'
        detail += ' Alignment workers disconnected; waiting for authenticated reconnection.'
    rate = None
    recent = []
    for job in done:
        try:
            finished = datetime.datetime.fromisoformat(job['finished']).timestamp()
            if 0 <= time.time() - finished <= 600: recent.append((finished, job))
        except (KeyError, TypeError, ValueError): pass
    recent.sort(key=lambda row: row[0])
    if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 60 and time.time() - recent[-1][0] < 180:
        rate = sum(sum(b-a for a,b in zip(j['start_offsets'],j['end_offsets'])) for _,j in recent[1:]) / (recent[-1][0]-recent[0][0])
    percent = round(100 * completed / total, 2) if total else None
    measured = {'completed': len(done), 'total': len(jobs), 'work_percent': percent,
                'completed_units': completed, 'total_units': total, 'unit': 'input bytes',
                'label': 'input aligned', 'count_label': 'verified batches; more batches are streamed as needed',
                'basis': 'Verified aligned bytes across the complete input; excludes merge and final verification'}
    if rate: measured['units_per_second'] = rate
    eta = max(0, (total-completed)/rate) if rate and not alignment_done else None
    if rate:
        detail += f' Recent alignment throughput: {rate/1e6:.1f} MB/s across active workers. Estimate excludes merge and final verification.'
    try: index_log = (root/'bwa-index.stderr').read_text()[-32768:]
    except OSError: index_log = ''
    text_length = re.search(r'textLength=(\d+)', index_log)
    indexed = re.findall(r'(\d+) characters processed', index_log)
    index_detail = 'CodeCollective and Inspiron begin mapping automatically after index preparation and transfer.'
    if text_length and indexed:
        index_detail = f'BWT construction: {100*int(indexed[-1])/int(text_length[1]):.1f}%. Suffix-array construction and validation follow. ' + index_detail
    children = [
        {'id': 'reference', 'name': 'Whole-genome reference index', 'status': 'completed' if (root / 'reference/verified.json').exists() else 'running' if index.get('phase') == 'running' else 'queued', 'worker': 'compai.local'},
        {'id': 'compact-reference', 'name': 'Lower-memory reference index for additional workers', 'status': 'completed' if (root / 'reference/bwa-verified.json').exists() else 'running' if bwa.get('phase') == 'running' and service_active else 'waiting', 'worker': 'compai.local', 'detail': index_detail},
        {'id': 'alignment', 'name': 'Align paired reads across the whole genome', 'status': 'completed' if alignment_done else status, 'completed_units': completed, 'total_units': total, 'unit': 'input bytes', 'worker': ', '.join(hosts) or 'waiting for worker', 'detail': detail, **({'execution_eta_seconds':eta,'eta_scope':'Alignment'} if eta is not None else {})},
        {'id': 'gpu-qc', 'name': 'Physical GPU read-quality checks', 'status': 'completed' if complete else 'running' if service_active and gpu_hosts else 'queued', 'worker': ', '.join(sorted(h for h in gpu_hosts if h)), 'detail': f'{len(gpu_hosts)} machines passed real-read GPU checks against CPU counts. Alignment uses CPUs; GPUs perform quality checks.'},
        {'id': 'finalize', 'name': 'Merge, mark duplicates, and verify final alignment', 'status': 'completed' if complete else 'running' if read(root / 'finalization.json').get('phase') == 'running' else 'waiting_dependency', 'worker': 'compai.local', 'depends_on':['alignment'], 'waiting_label':'Queued until all alignment batches complete' if not alignment_done else 'Awaiting finalization resources', 'detail':'Queued until all alignment batches complete; this does not block ongoing alignment.' if not alignment_done else 'All alignment batches complete; awaiting finalization resources.'},
    ]
    workers = sorted(set(state.get('workers',{})) | set(j.get('host') for j in jobs if j.get('host')) | gpu_hosts)
    for host in workers:
        active_count = sum(j.get('host')==host for j in active)
        finished_reads = 2*sum(j.get('pairs',0) for j in done if j.get('host')==host)
        phase = state.get('workers',{}).get(host,{}).get('phase','')
        note = f'{finished_reads:,} reads aligned; {active_count} active batches.'
        if active_count: worker_status='running'
        elif phase=='waiting_lower_memory_bwa_index':
            worker_status='preparing' if (root/'reference/bwa-verified.json').exists() else 'waiting_dependency'
            note+=' Preparing this worker with the completed reference.' if worker_status=='preparing' else ' Queued until lower-memory reference index is ready.'
        elif phase=='paused': worker_status='paused'; note+=' New compute jobs paused by owner.'
        elif phase=='transferring_reference': worker_status='preparing'; note+=' Transferring and verifying this worker’s reference copy.'
        elif phase=='gpu_quality_checks': worker_status='waiting'; note+=' Assigned GPU quality checks as batches become available.'
        elif phase=='waiting_memory_or_cpus': worker_status='waiting_capacity'; note+=' Waiting for available CPU or RAM.'
        elif phase in ('unavailable', 'reconnecting'): worker_status='waiting'; note+=' Disconnected; retrying authenticated worker connection.'
        else: worker_status='waiting'; note+=' Preparing reference, transferring inputs, or awaiting the next batch.'
        children.append({'id':'worker-'+host.replace('.','-'),'name':'Worker · '+host,'status':'completed' if complete else worker_status if service_active else 'interrupted','worker':host,'detail':note,'waiting_label':'Queued until lower-memory reference index is ready' if worker_status=='waiting_dependency' else None})
    samples=read(root/'bottleneck-samples.json').get('samples',{})
    phases=phase_summary(root,jobs)
    quality_times=[j['gpu_qc']['phase_seconds'] for j in sorted(done,key=lambda j:j.get('finished') or '') if (j.get('gpu_qc') or {}).get('phase_seconds')][-30:]
    if quality_times:
        quality_note=f"Typical quality-check batch ({len(quality_times)} samples): {statistics.median(v['read_parse_and_prepare'] for v in quality_times):.1f}s reading/preparation; {statistics.median(v['gpu_requests_including_queue'] for v in quality_times):.1f}s GPU requests including queue time."
        next(c for c in children if c['id']=='gpu-qc')['detail']+=' '+quality_note
    waiting_hosts=[h for h,v in state.get('workers',{}).items() if v.get('phase')=='waiting_lower_memory_bwa_index' and h not in hosts and not (root/'reference/bwa-verified.json').exists()]
    def observed(label,evidence):return {'label':label,'confidence':'observed','evidence':evidence}
    for child in children:
        ident=child['id']
        if child['status']=='completed':child['bottleneck']=observed('Complete','No unfinished work in this stage.')
        elif child['status']=='interrupted':child['bottleneck']=observed('Controller stopped','Saved checkpoints retained; controller must resume.')
        elif ident=='finalize':child['bottleneck']=observed('Alignment dependency' if not alignment_done else 'Finalization resources',child['waiting_label'])
        elif ident=='compact-reference':child['bottleneck']=classify(samples.get(ident,{}))
        elif ident=='gpu-qc':child['bottleneck']={'label':'Follows incoming read batches','confidence':'workflow','evidence':'GPU checks run as reads arrive. No task-specific GPU saturation measurement is available.'}
        elif ident=='alignment':
            details=[classify(samples.get('alignment:'+h,{})) for h in hosts]
            child['bottleneck']=observed('Worker connections', 'Waiting for authenticated reconnection: '+', '.join(reconnecting)) if status=='waiting' and reconnecting else observed('Reference preparation limits fleet participation',', '.join(waiting_hosts)+' await the lower-memory index.') if waiting_hosts else {'label':'Worker throughput','confidence':'unavailable','evidence':'; '.join(d['label']+': '+d['evidence'] for d in details) or 'Awaiting active worker measurements.'}
            if phases.get('sample_count'):
                labels={'reference_setup':'Reference loading and setup','mapping_pipeline':'Read mapping pipeline','mapper_cleanup':'Mapper cleanup'}
                child['bottleneck']=observed(labels[phases['dominant_phase']],f"{phases['dominant_percent']}% of measured mapper time across {phases['sample_count']} recent batches. Transfers, GPU checks, and overlapping sorting are outside this measurement.")
        elif ident.startswith('worker-'):
            host=child['worker']
            if host in waiting_hosts:child['bottleneck']=observed('Reference index dependency','Waiting for lower-memory reference preparation, then transfer.')
            elif host in hosts:
                child['bottleneck']=classify(samples.get('alignment:'+host,{}))
                if host in phases.get('current',{}):child['current_phase']=phases['current'][host]
            elif state.get('workers',{}).get(host,{}).get('phase')=='gpu_quality_checks':child['bottleneck']={'label':'GPU task timing not measured','confidence':'unavailable','evidence':'This worker handles scheduled quality checks; per-task GPU execution and queue timing are not yet instrumented.'}
            else:child['bottleneck']={'label':'Current stage not measured','confidence':'unavailable','evidence':child.get('detail','Awaiting task-specific instrumentation.')}
        else:child['bottleneck']={'label':'Not measured','confidence':'unavailable','evidence':'Awaiting stage measurements.'}
    return {'id': 'whole-genome-read-alignment-20261101', 'name': 'Whole-genome read alignment to HuRef2',
            'status': status, 'detail': detail, 'updated': time.time(), 'children': children,
            'primary_progress':measured,
            'phase_timing':phases,
            'fleet_constraint':', '.join(waiting_hosts)+' await the lower-memory reference index.' if waiting_hosts else None,
            'bottleneck':next(c['bottleneck'] for c in children if c['id']=='alignment'),
            **({'execution_eta_seconds':eta,'eta_scope':'Alignment'} if eta is not None and service_active else {}),
            'privacy': 'Operational counters only; individual reads, identifiers, and genetic results excluded.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True); parser.add_argument('--once', action='store_true'); args = parser.parse_args()
    args.registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'lifetech-julian-huref2-alignment.service']).returncode == 0
        value = snapshot(args.root, active); path = args.registry / (value['id'] + '.json')
        tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(value)); tmp.chmod(0o600); tmp.replace(path)
        if args.once: return
        time.sleep(2)


if __name__ == '__main__': main()
