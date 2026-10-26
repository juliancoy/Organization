"""Private BAM coverage, gVCF span audit and reproducible offline VEP refresh."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from scripts.genomics import private_run
from scripts.launcher_agent import ResourceLease


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def gvcf_spans(source):
    """Audit represented positions; missing confidence stays unknown, never imputed."""
    totals = Counter()
    ends = {}
    previous = {}
    with gzip.open(source, 'rt') if str(source).endswith('.gz') else open(source) as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) != 10:
                raise ValueError('Expected one gVCF sample')
            chrom, pos = fields[0], int(fields[1])
            if pos < previous.get(chrom, 0):
                raise ValueError('gVCF is not coordinate sorted')
            previous[chrom] = pos
            info = dict(item.split('=', 1) for item in fields[7].split(';') if '=' in item)
            end = int(info.get('END', pos))
            if end < pos:
                raise ValueError('Invalid gVCF span')
            sample = dict(zip(fields[8].split(':'), fields[9].split(':')))
            gt = sample.get('GT', '.').replace('|', '/').split('/')
            def number(value):
                return None if value in (None, '.') else float(value)
            # MinDP describes the block minimum; FORMAT DP alone is not a
            # lower bound for every position in a reference block.
            dp = number(info.get('MinDP') if end > pos else sample.get('DP'))
            gq = number(sample.get('GQ'))
            categories = ['represented_bases']
            if gt and all(allele != '.' for allele in gt):
                categories.append('called_genotype_span')
                if dp is not None and dp >= 20:
                    categories.append('called_min_depth_20_span')
                    if gq is not None and gq >= 20 and fields[6] == 'PASS':
                        categories.append('pass_depth_20_gq_20_span')
            if dp is None:
                categories.append('depth_confidence_unknown_span')
            if gq is None:
                categories.append('genotype_quality_unknown_span')
            for category in categories:
                key = (category, chrom)
                totals[category] += max(0, end - max(pos - 1, ends.get(key, 0)))
                ends[key] = max(end, ends.get(key, 0))
            totals['records'] += 1
    return {'counts': dict(totals), 'scope': 'Union of represented gVCF positions, including END reference blocks. Categories overlap. Reference blocks require MinDP for minimum-depth confidence. Missing GQ is unknown. This is not a validated genome-wide callable-region mask.'}


def coverage_summary(source):
    rows = []
    for line in source.read_text().splitlines():
        if line.startswith('#') or not line:
            continue
        fields = line.split('\t')
        rows.append({'contig': fields[0], 'length': int(fields[2]) - int(fields[1]) + 1,
                     'covered_bases': int(fields[4]), 'mean_depth': float(fields[6])})
    groups = {}
    for label, selected in [('all_bam_contigs', rows), ('autosomes', [r for r in rows if r['contig'].removeprefix('chr') in {str(i) for i in range(1, 23)}])]:
        length = sum(r['length'] for r in selected)
        groups[label] = {'reference_bases': length,
                         'covered_bases': sum(r['covered_bases'] for r in selected),
                         'mean_depth': sum(r['mean_depth'] * r['length'] for r in selected) / length if length else None}
    return {'groups': groups, 'contigs': rows, 'scope': 'BAM reference lengths include gaps and ambiguous bases; no accessibility mask. MQ >=20, BQ >=20; unmapped, secondary, QC-failed, duplicate and supplementary reads excluded. Overlapping mates are not collapsed. Coverage means >=1 qualifying base observation, not clinical callability.'}


def variant_identity(source):
    digest = hashlib.sha256()
    records = annotated = 0
    with gzip.open(source, 'rt') if str(source).endswith('.gz') else open(source) as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            digest.update(('\t'.join(fields[:5] + fields[8:]) + '\n').encode())
            records += 1
            annotated += any(item.startswith('CSQ=') for item in fields[7].split(';'))
    return {'records': records, 'variant_and_genotype_sha256': digest.hexdigest(), 'records_with_csq': annotated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('bam', 'gvcf', 'called', 'cache', 'reference', 'output', 'resource-dir'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--image', required=True, help='Immutable local VEP 115.2 image ID')
    args = parser.parse_args()
    if not args.image.startswith('sha256:'):
        raise ValueError('Pin the annotation image by immutable ID')
    os.umask(0o077)
    folder = private_run(args.output)
    print(folder, flush=True)
    state = {'status': 'running', 'started': stamp(), 'jobs': {}}
    lock = threading.Lock()
    annotation_container = 'genomics-vep-' + uuid.uuid4().hex
    def update(name, **values):
        with lock:
            state['jobs'].setdefault(name, {}).update(values)
            atomic_json(folder / 'state.json', state)
    @contextmanager
    def reserve(name, cpus, memory, container=''):
        lease = ResourceLease('genomics-followup', cpus, memory, container=container, directory=args.resource_dir, pids=128)
        try:
            update(name, status='waiting_for_capacity')
            while not lease.try_acquire():
                time.sleep(1)
            update(name, status='running', started=stamp())
            yield
        finally:
            lease.close()
    def run(name, cpus, memory, action):
        try:
            with reserve(name, cpus, memory, annotation_container if name == 'fresh_offline_vep' else ''):
                action()
            update(name, status='passed', finished=stamp())
        except Exception as error:
            update(name, status='failed', error=type(error).__name__, finished=stamp())
            raise
    def coverage():
        command = ['samtools', 'coverage', '-q', '20', '-Q', '20', '--ff', '3844', '-d', '0', '-o', str(folder / 'bam-coverage.tsv'), str(args.bam)]
        with (folder / 'coverage.stderr').open('x') as log:
            subprocess.run(command, stderr=log, check=True)
        atomic_json(folder / 'coverage-summary.json', coverage_summary(folder / 'bam-coverage.tsv'))
    def spans():
        atomic_json(folder / 'gvcf-spans.json', gvcf_spans(args.gvcf))
    def annotation():
        name = annotation_container
        command = ['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--log-driver', 'none', '--cpus', '4', '--memory', '12g', '--memory-swap', '12g', '--pids-limit', '128', '--user', str(os.getuid()) + ':' + str(os.getgid()), '--tmpfs', '/tmp:rw,noexec,nosuid,size=512m', '-v', str(args.cache.resolve()) + ':/cache:ro', '-v', str(args.called.resolve().parent) + ':/input:ro', '-v', str(args.reference.resolve().parent) + ':/reference:ro', '-v', str(folder) + ':/output:rw', args.image, 'vep', '--offline', '--cache', '--cache_version', '115', '--dir_cache', '/cache', '--assembly', 'GRCh38', '--fasta', '/reference/' + args.reference.name, '--format', 'vcf', '--vcf', '--input_file', '/input/' + args.called.name, '--output_file', '/output/fresh.vep.vcf', '--fork', '4', '--symbol', '--canonical', '--biotype', '--numbers', '--total_length', '--variant_class', '--sift', 'b', '--polyphen', 'b', '--af', '--af_gnomade', '--af_gnomadg', '--check_existing', '--no_stats']
        atomic_json(folder / 'vep-command.json', command)
        with (folder / 'vep.log').open('x') as log:
            try:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
            finally:
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['bgzip', '-@', '3', str(folder / 'fresh.vep.vcf')], check=True)
        subprocess.run(['bcftools', 'index', str(folder / 'fresh.vep.vcf.gz')], check=True)
        before, after = variant_identity(args.called), variant_identity(folder / 'fresh.vep.vcf.gz')
        matched = all(before[key] == after[key] for key in ('records', 'variant_and_genotype_sha256'))
        atomic_json(folder / 'annotation-identity.json', {'passed': matched, 'input': before, 'output': after})
        if not matched:
            raise ValueError('Annotation changed variant or genotype identity')
        # Fresh private annotations and QC can use every eligible enrolled node.
        subprocess.run([str(Path.home() / '.local/share/swarm-control/venv/bin/python'), '-m', 'scripts.genomics_distributed', '--qc', str(args.called), '--annotations', str(folder / 'fresh.vep.vcf.gz'), '--output', str(args.output.parent / 'distributed'), '--slots', '0'], check=True)
    provenance = {'started': stamp(), 'image': args.image, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'samtools': subprocess.check_output(['samtools', '--version'], text=True, errors='replace').splitlines()[0], 'sources': {key: {'path': str(path.resolve()), 'bytes': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns} for key, path in vars(args).items() if key in ('bam', 'gvcf', 'called', 'reference')}, 'cache_info_sha256': hashlib.sha256((args.cache / 'homo_sapiens/115_GRCh38/info.txt').read_bytes()).hexdigest(), 'scope': 'Private descriptive research. Cache release 115 includes historical clinical assertions; no diagnosis or current clinical classification.'}
    atomic_json(folder / 'provenance.json', provenance)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, 'bam_coverage', 1, 2048, coverage), pool.submit(run, 'gvcf_span_audit', 1, 1024, spans), pool.submit(run, 'fresh_offline_vep', 4, 12288, annotation)]
        failures = []
        for future in futures:
            try:
                future.result()
            except Exception as error:
                failures.append(type(error).__name__)
    state.update(status='failed' if failures else 'passed', finished=stamp())
    atomic_json(folder / 'state.json', state)
    if failures:
        raise RuntimeError('One or more private analyses failed; inspect private reports')


if __name__ == '__main__':
    main()
