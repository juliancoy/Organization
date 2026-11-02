"""Private, checkpointed paired-read alignment against the complete HuRef2 assembly.

One reference index per host, bounded pending inputs, authenticated resource leases,
and checksum-verified output collection. No individual genomic data is published.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import threading
import time

from scripts import genomics_distributed as distributed
from scripts.wgs_swarm_download import atomic_json, stamp, PREFLIGHT
from scripts.wgs_recall_swarm import leased_run, copy_receipt, AgentClient, GROUP
from scripts.wgs_finalize import attest, install, remote_json
from scripts.wgs_paired_shards import record, pair_name, sha
from docker_utils.images import ImageResolver

IMAGE = 'lifetech/genome-compare:20261031'
ROOT = Path(__file__).resolve().parents[1]


def identity(path):
    p = Path(path); s = p.stat()
    return {'path': str(p.resolve()), 'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns}


def quality_estimate(hist):
    total = sum(hist)
    if not total or any(type(n) is not int or n < 0 for n in hist) or len(hist) != 94:
        raise ValueError('Invalid Phred+33 histogram')
    return {'bases': total, 'predicted_error_fraction': sum(n * 10 ** (-q / 10) for q, n in enumerate(hist)) / total,
            'q30_fraction': sum(hist[30:]) / total,
            'scope': 'Quality-predicted base-call error, not measured truth-set accuracy or reference divergence'}


def make_batch(streams, folder, number, pairs):
    """Resume by byte offsets; never split mates or silently skip malformed input."""
    folder = Path(folder); folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    offsets = [s.tell() for s in streams]; hist = [0] * 94; count = 0
    paths = [folder / f'{number:06d}_{i}.fq.gz' for i in (1, 2)]
    partials = [Path(str(p) + '.partial') for p in paths]
    try:
        with gzip.open(partials[0], 'wb', compresslevel=1) as a, gzip.open(partials[1], 'wb', compresslevel=1) as b:
            for _ in range(pairs):
                reads = [record(s) for s in streams]
                if (reads[0] is None) != (reads[1] is None):
                    raise ValueError('Unequal mate counts')
                if reads[0] is None: break
                if pair_name(reads[0][0]) != pair_name(reads[1][0]):
                    raise ValueError('Mate identity mismatch')
                for read, out in zip(reads, (a, b)):
                    for q in read[3].rstrip(b'\r\n'):
                        if not 33 <= q <= 126: raise ValueError('Invalid Phred+33 quality')
                        hist[q - 33] += 1
                    out.write(b''.join(read))
                count += 1
        if not count:
            for p in partials: p.unlink()
            return None
        files = []
        for p, out in zip(partials, paths):
            p.chmod(0o600); p.replace(out)
            files.append({'path': str(out), 'bytes': out.stat().st_size, 'sha256': sha(out)})
        return {'id': f'{number:06d}', 'pairs': count, 'start_offsets': offsets,
                'end_offsets': [s.tell() for s in streams], 'files': files, 'quality_histogram': hist}
    except BaseException:
        for p in partials: p.unlink(missing_ok=True)
        raise


def command(host, args, **kwargs):
    return subprocess.run(distributed.shell_command(host, args), check=True, **kwargs)


def capacity(host):
    c = AgentClient(host)
    try:
        if not c.call('worker.policy').get('accepting_jobs'):
            raise ConnectionError('Owner paused worker jobs')
        return c.call('resource.capacity', {'group': GROUP})
    finally: c.close()


def prepare(root, local, manifest):
    reference = root / 'reference'; reference.mkdir(mode=0o700, exist_ok=True)
    fasta = reference / 'reference.fa'; index = reference / 'reference.sr.mmi'
    if not fasta.exists():
        tmp = Path(str(fasta) + '.partial')
        with gzip.open(manifest['assembly'], 'rb') as src, tmp.open('wb') as dst:
            shutil.copyfileobj(src, dst, 8 * 1024**2)
        tmp.replace(fasta)
    if not (reference / 'verified.json').exists():
        atomic_json(root / 'phase.json', {'phase': 'building_whole_genome_reference_index', 'updated': stamp()})
        leased_run(local, manifest['image'],
                   'minimap2 -x sr -I 16G -t 4 -d /ref/reference.sr.mmi.partial /ref/reference.fa && '
                   'samtools faidx /ref/reference.fa && mv /ref/reference.sr.mmi.partial /ref/reference.sr.mmi',
                   [(reference, '/ref', False), (root / 'tmp', '/work', False)], str(root / 'tmp'),
                   4, 24000, root / 'reference-index.json', wait_for_capacity=True)
        atomic_json(reference / 'verified.json', {'files': {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)}
                                                          for p in (fasta, Path(str(fasta) + '.fai'), index)},
                                                 'image': manifest['image'], 'preset': 'sr', 'single_index_batch': True})
    verified = json.loads((reference / 'verified.json').read_text())
    for name, f in verified['files'].items():
        if (reference / name).stat().st_size != f['bytes'] or sha(reference / name) != f['sha256']:
            raise ValueError('Reference changed')
    return reference, verified


def reference_on(host, local, root, reference, verified):
    if host['local']: return str(reference)
    target = str(Path(host['cache']).parent / 'huref2-shortread-reference')
    command(host, ['mkdir', '-p', '-m', '700', target])
    for name, f in verified['files'].items():
        receipt = copy_receipt(local, host, {**f, 'path': str(reference / name)}, resume=True)
        code = "import pathlib,os,sys;p=pathlib.Path(sys.argv[2]);t=pathlib.Path(str(p)+'.partial');t.unlink(missing_ok=True);os.link(sys.argv[1],t);os.replace(t,p)"
        command(host, ['python3', '-c', code, receipt['path'], target + '/' + name])
    return target


def validate_counts(flag, pairs):
    row = flag['QC-passed reads']; failed = flag['QC-failed reads']
    if row['primary'] + failed['primary'] != 2 * pairs:
        raise ValueError('Alignment lost or duplicated primary reads')


def supervise_worker(name, run, discover, stop, publish, finished):
    """Reauthenticate and refresh enrolled endpoints after a worker disconnects."""
    retries = 0
    while not stop.is_set() and not finished():
        try:
            publish({'phase': 'reconnecting', 'reconnects': retries, 'updated': stamp()})
            candidates = discover(False, names={name})
            host = next((h for h in candidates if h['host'] == name and h.get('eligible')), None)
            if host is None:
                raise ConnectionError(next((h.get('reason') for h in candidates), 'Enrolled worker unavailable'))
            publish({'phase': 'preparing', 'reconnects': retries, 'updated': stamp()})
            run(host)
            if finished() or stop.is_set(): return
            raise ConnectionError('Worker exited before queued alignment completed')
        except Exception as error:
            retries += 1
            delay = min(60, 5 * 2 ** min(retries - 1, 4))
            publish({'phase': 'reconnecting', 'reconnects': retries, 'error': str(error),
                     'retry_seconds': delay, 'updated': stamp()})
            stop.wait(delay)


def connection_failure(error):
    text = str(error).lower()
    return isinstance(error, (ConnectionError, TimeoutError)) or any(term in text for term in (
        'connection refused', 'connection reset', 'connection aborted', 'connection closed',
        'remote end closed', 'no route to host', 'not responding', 'timed out',
        'broken pipe', 'network is unreachable', 'could not resolve hostname'))


def main(root):
    os.umask(0o077); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (root / 'controller.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((root / 'manifest.json').read_text())
    for src in manifest['reads']:
        if identity(src['path']) != src: raise ValueError('Source identity changed')
    if sha(Path(manifest['assembly'])) != manifest['assembly_sha256']: raise ValueError('Assembly identity changed')
    hosts = distributed.discover(False); local = next(h for h in hosts if h.get('local') and h['eligible'])
    local['cache'] = str(root / 'collected'); local['container_user'] = f'{os.getuid()}:{os.getgid()}'
    (root / 'tmp').mkdir(mode=0o700, exist_ok=True)
    reference, verified = prepare(root, local, manifest)
    # Measured pilot RSS exceeds on-disk index size: do not derive admission
    # solely from file bytes (that under-reserves the minimizer tables).
    minimum_memory = max(13032, math.ceil(verified['files']['reference.sr.mmi']['bytes'] / 1024**2 * 1.3) + 2048)
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'jobs': {}, 'producer_complete': False}
    for j in state['jobs'].values():
        if j['phase'] == 'running': j['phase'] = 'pending'
    guard = threading.RLock(); stop = threading.Event(); errors = []
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop.set())
    def save():
        with guard:
            state['updated'] = stamp(); atomic_json(state_path, state)
    def build_bwa():
        try:
            if (reference / 'bwa-verified.json').exists(): return
            cmd = 'bwa index /ref/reference.fa'
            leased_run(local, manifest['image'], cmd, [(reference, '/ref', False), (root / 'tmp', '/work', False)], str(root / 'tmp'),
                       1, 8000, root / 'bwa-index.json', wait_for_capacity=True)
            names = ['reference.fa', 'reference.fa.fai'] + ['reference.fa.' + ext for ext in ('amb', 'ann', 'bwt', 'pac', 'sa')]
            atomic_json(reference / 'bwa-verified.json', {'files': {name: {'bytes': (reference / name).stat().st_size, 'sha256': sha(reference / name)} for name in names}})
        except Exception as e:
            with guard: state['bwa_index_error'] = str(e); save()
    def gpu_root_on(host):
        if host['local']: return '/mnt/PILEDRIVE/genomics-workbench/wgs/gpu-qc'
        home = command(host, ['python3', '-c', 'import pathlib;print(pathlib.Path.home())'], capture_output=True, text=True).stdout.strip()
        return home + '/.local/state/lifetech-launcher/gpu-qc'
    def run_qc(host, work, incoming, job):
        install(host, work + '/read_qc.py', (ROOT / 'scripts/julian_read_qc.py').read_bytes())
        gpu = json.loads(command(host, ['python3', work + '/read_qc.py', '--gpu-root', gpu_root_on(host), *[f['path'] for f in incoming]], capture_output=True, text=True).stdout)
        if gpu['quality_histogram'] != job['quality_histogram']: raise ValueError('GPU QC disagrees with source quality histogram')
        return gpu
    def qc_worker(host):
        while not stop.is_set():
            with guard:
                job = next((j for j in state['jobs'].values() if j.get('qc_required') and j.get('qc_phase') != 'complete'), None)
                if not job and state['producer_complete']: return
            if not job: stop.wait(2); continue
            incoming = []
            try:
                incoming = [copy_receipt(local, host, f, resume=True) for f in job['files']]
                work = str(Path(host['cache']).parent / 'qc' / job['id'])
                command(host, ['mkdir', '-p', '-m', '700', work])
                gpu = run_qc(host, work, incoming, job)
                with guard: job.update(qc_phase='complete', independent_gpu_qc={**gpu, 'host': host['host']}); save()
            except Exception as e:
                # Source CPU histogram remains authoritative; GPU unavailability
                # must never block alignment or silently claim acceleration.
                with guard: job.update(qc_required=False, qc_phase='unavailable', qc_error=str(e)); save()
            finally:
                for f in incoming: command(host, ['rm', '-f', '--', f['path']])
    def worker(host):
        try:
            if not host.get('eligible'): raise RuntimeError(host.get('reason', 'Unavailable'))
            if host['local']:
                host.update(cache=local['cache'], container_user=local['container_user'])
            if not host['local']:
                requested = '~/.local/state/lifetech-launcher/private-julian-huref2/' + root.name + '/cache'
                result = command(host, ['python3', '-c', PREFLIGHT, host['machine'], requested, '0'], capture_output=True, text=True)
                host.update(json.loads(result.stdout))
            resolver = ImageResolver([], lambda h: h['ssh'])
            resolver.ensure(IMAGE, [] if host['local'] else [host])
            image = resolver.inspect(None if host['local'] else host, IMAGE)
            if resolver.fingerprint(None if host['local'] else host, IMAGE) != resolver.fingerprint(None, manifest['image']):
                raise ValueError('Unapproved alignment image contents')
            mapper = 'minimap2'
            required_memory = minimum_memory
            host_verified = verified
            if capacity(host)['available_mb'] < minimum_memory:
                with guard:
                    state.setdefault('workers', {})[host['host']] = {'phase': 'gpu_quality_checks' if host['host'] == 'julian-minipc.local' else 'waiting_lower_memory_bwa_index', 'required_mb': minimum_memory}; save()
                if host['host'] == 'julian-minipc.local':
                    qc_worker(host); return
                mapper = 'bwa'; required_memory = 7000
                while not stop.is_set() and not (reference / 'bwa-verified.json').exists(): stop.wait(5)
                if stop.is_set(): return
                host_verified = json.loads((reference / 'bwa-verified.json').read_text())
            ref = reference_on(host, local, root, reference, host_verified)
            while not stop.is_set():
                with guard:
                    if state['producer_complete'] and all(j['phase'] == 'complete' for j in state['jobs'].values()): return
                limits = capacity(host)
                if limits['available_mb'] < required_memory or limits.get('available_cpus', 0) < 2:
                    with guard:
                        state.setdefault('workers', {})[host['host']] = {'phase': 'waiting_memory_or_cpus', 'required_mb': required_memory, 'available_mb': limits['available_mb']}; save()
                    stop.wait(10); continue
                with guard:
                    job = next((j for j in state['jobs'].values() if j['phase'] == 'pending' and (not j.get('qc_required') or j.get('qc_phase') == 'complete')), None)
                    if job: job.update(phase='running', host=host['host']); save()
                    elif state['producer_complete']: return
                if not job: stop.wait(2); continue
                try:
                    incoming = [copy_receipt(local, host, f, resume=True) for f in job['files']]
                    work = str(Path(host['cache']).parent / 'alignments' / job['id'])
                    command(host, ['mkdir', '-p', '-m', '700', work + '/tmp'])
                    gpu = None
                    probe = subprocess.run(distributed.shell_command(host, ['test', '-S', gpu_root_on(host) + '/service.sock']), capture_output=True)
                    if probe.returncode == 0:
                        try: gpu = run_qc(host, work, incoming, job)
                        except ValueError: raise
                        except Exception as error:
                            if connection_failure(error): raise
                            with guard: job.update(qc_error=str(error)); save()
                    cpus = min(max(2, int(host['cpus']) - 1), int(limits['available_cpus']))
                    memory = min(limits['available_mb'], required_memory + 2048)
                    rg = '@RG\\tID:Julian-' + job['id'] + '\\tSM:Julian\\tLB:Julian-WGS'
                    args = ['minimap2', '-ax', 'sr', '-K', '10m', '-t', str(cpus - 1), '-R', rg, '/ref/reference.sr.mmi', '/mate1.fq.gz', '/mate2.fq.gz'] if mapper == 'minimap2' else ['bwa', 'mem', '-t', str(cpus - 1), '-R', rg, '/ref/reference.fa', '/mate1.fq.gz', '/mate2.fq.gz']
                    cmd = shlex.join(args)
                    cmd += ' | samtools sort -m 512M -T /work/tmp/sort -o /work/alignment.partial.bam -'
                    cmd += ' && samtools quickcheck /work/alignment.partial.bam && samtools flagstat -O json /work/alignment.partial.bam > /work/flagstat.json'
                    cmd += ' && samtools stats /work/alignment.partial.bam > /work/stats.txt && mv /work/alignment.partial.bam /work/alignment.bam'
                    mounts = [(ref, '/ref', True), (work, '/work', False)] + [(f['path'], f'/mate{i}.fq.gz', True) for i, f in enumerate(incoming, 1)]
                    leased_run(host, image, cmd, mounts, work, cpus, memory, root / 'attempts' / (host['host'].replace('.', '_') + '-' + job['id'] + '.json'))
                    flag = remote_json(host, work + '/flagstat.json'); validate_counts(flag, job['pairs'])
                    receipt = attest(host, work + '/alignment.bam'); collected = copy_receipt(host, local, receipt, resume=True)
                    out = root / 'bams' / (job['id'] + '.bam'); out.parent.mkdir(mode=0o700, exist_ok=True)
                    out.unlink(missing_ok=True); os.link(collected['path'], out)
                    stats = command(host, ['cat', work + '/stats.txt'], capture_output=True).stdout
                    (out.parent / (job['id'] + '.stats.txt')).write_bytes(stats)
                    with guard:
                        job.pop('error', None)
                        job.update(phase='complete', receipt={**receipt, 'path': str(out)}, flagstat=flag, mapper=mapper, gpu_qc=gpu, finished=stamp()); save()
                        state.setdefault('workers', {})[host['host']] = {'phase': 'ready', 'updated': stamp()}; save()
                    # Only this run's validated, reproducible temporary inputs/output.
                    for f in job['files']: Path(f['path']).unlink(missing_ok=True)
                    if not host['local']:
                        command(host, ['rm', '-f', '--', work + '/alignment.bam', *[f['path'] for f in incoming]])
                except distributed.CapacityWait:
                    with guard: job['phase'] = 'pending'; save()
                    stop.wait(5)
                except Exception as e:
                    with guard:
                        # A validated batch remains complete even if remote cleanup disconnects.
                        if job['phase'] != 'complete':
                            job.update(phase='pending', error=str(e))
                            if not connection_failure(e):
                                job['attempts'] = job.get('attempts', 0) + 1
                                if job['attempts'] >= 3: job['phase'] = 'needs_attention'
                        save()
                    if connection_failure(e): raise
                    stop.wait(10)
        except Exception as e:
            with guard: state.setdefault('workers', {})[host['host']] = {'phase': 'unavailable', 'error': str(e)}; save()
            raise
    def finished():
        with guard:
            return state['producer_complete'] and all(j['phase'] == 'complete' for j in state['jobs'].values())
    def supervised(host):
        def publish(value):
            with guard: state.setdefault('workers', {})[host['host']] = value; save()
        supervise_worker(host['host'], worker, distributed.discover, stop, publish, finished)
    pool = ThreadPoolExecutor(max_workers=len(hosts) + 1); index_future = pool.submit(build_bwa)
    futures = [pool.submit(supervised, h) for h in hosts]
    try:
        if not state['producer_complete']:
            previous = list(state['jobs'].values())
            number = len(previous); offsets = previous[-1]['end_offsets'] if previous else [0, 0]
            with open(manifest['reads'][0]['path'], 'rb') as a, open(manifest['reads'][1]['path'], 'rb') as b:
                for s, offset in zip((a, b), offsets): s.seek(offset)
                while True:
                    if stop.is_set(): raise InterruptedError('Controller stopped; completed batches retained')
                    with guard:
                        outstanding = sum(j['phase'] != 'complete' for j in state['jobs'].values())
                        failed = any(j['phase'] == 'needs_attention' for j in state['jobs'].values())
                    if failed: raise RuntimeError('Repeated job failure; evidence preserved')
                    if all(f.done() for f in futures): raise RuntimeError('No alignment workers remain')
                    if outstanding >= 2 * len(hosts) or shutil.disk_usage(root).free < 4 * 1024**3:
                        time.sleep(5); continue
                    batch = make_batch((a, b), root / 'shards', number, manifest['pairs_per_batch'])
                    if batch is None: break
                    minipc = any(h['host'] == 'julian-minipc.local' and h.get('eligible') for h in hosts)
                    with guard: state['jobs'][batch['id']] = {**batch, 'phase': 'pending', 'qc_required': minipc and number % 20 == 0}; save()
                    number += 1
            with guard: state['producer_complete'] = True; save()
        while True:
            if stop.is_set(): raise InterruptedError('Controller stopped; completed batches retained')
            with guard:
                jobs = list(state['jobs'].values())
                if any(j['phase'] == 'needs_attention' for j in jobs): raise RuntimeError('Repeated alignment failure')
                if all(j['phase'] == 'complete' for j in jobs): break
            if all(f.done() for f in futures): raise RuntimeError('All workers exited before completion')
            time.sleep(5)
        hist = [sum(j['quality_histogram'][q] for j in jobs) for q in range(94)]
        offsets = [0, 0]
        for j in jobs:
            if j['start_offsets'] != offsets: raise ValueError('Gap or overlap in source read batches')
            offsets = j['end_offsets']
        if offsets != [src['bytes'] for src in manifest['reads']]: raise ValueError('Source files not completely accounted for')
        for src in manifest['reads']:
            if identity(src['path']) != src: raise ValueError('Source changed during run')
        atomic_json(root / 'alignment-summary.json', {'phase': 'all_batches_aligned', 'pairs': sum(j['pairs'] for j in jobs),
                    'batches': len(jobs), 'quality': quality_estimate(hist), 'finished': stamp(),
                    'duplicate_marking': 'pending', 'reference_divergence_is_not_sequencing_error': True,
                    'aligner_batch_counts': {m: sum(j.get('mapper', 'minimap2') == m for j in jobs) for m in ('minimap2', 'bwa')},
                    'downstream_validation': 'Stratify mismatch statistics by aligner; cross-aligner and repeat validation required before genotype conclusions',
                    'dat1_genotype': 'not_established_by_alignment'})
        final = root / 'final'; final.mkdir(mode=0o700, exist_ok=True)
        if not (final / 'complete.json').exists():
            from scripts.julian_finalize import finalize
            # Sample capacity after mapping finishes; RAM scratch is part of the
            # acquired memory reservation, never unbounded shared host tmpfs.
            while not stop.is_set():
                limits=capacity(local)
                if limits['available_mb']>=8192 and limits.get('available_cpus',0)>=5:break
                atomic_json(root/'finalization.json',{'phase':'waiting_capacity','stage':'RAM streaming finalization','updated':stamp()})
                stop.wait(5)
            if stop.is_set():raise InterruptedError('Controller stopped; completed batches retained')
            finalize(root,jobs,local,manifest,reference,save,int(limits['available_mb']),stop=stop)

    finally:
        stop.set(); pool.shutdown(wait=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', type=Path, required=True)
    main(parser.parse_args().root)
