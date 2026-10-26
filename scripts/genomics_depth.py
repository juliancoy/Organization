"""Private whole-BAM depth distributions with explicit denominators and filters."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from scripts.genomics_followup import atomic_json, stamp
from scripts.launcher_agent import ResourceLease

THRESHOLDS = (1, 10, 20, 30, 50)


def windows(contigs, size=100000):
    for chrom, length in contigs:
        for start in range(0, length, size):
            yield chrom, start, min(start + size, length)


def summarize(path, contigs):
    expected = iter(windows(contigs))
    totals = defaultdict(lambda: {str(t): 0 for t in THRESHOLDS})
    with gzip.open(path, 'rt') as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            fields = line.rstrip().split('\t')
            actual = (fields[0], int(fields[1]), int(fields[2]))
            if actual != next(expected, None):
                raise ValueError('Depth intervals differ from the full BAM reference dictionary')
            counts = [int(v) for v in fields[4:]]
            if len(counts) != len(THRESHOLDS) or counts != sorted(counts, reverse=True) or min(counts) < 0 or max(counts) > actual[2] - actual[1]:
                raise ValueError('Invalid depth threshold counts')
            for threshold, count in zip(THRESHOLDS, counts):
                totals[actual[0]][str(threshold)] += count
    if next(expected, None) is not None:
        raise ValueError('Depth output is missing reference intervals')
    result = {}
    for label, selected in [('all_bam_contigs', contigs), ('autosomes', [(c, n) for c, n in contigs if c.removeprefix('chr') in {str(i) for i in range(1, 23)}])]:
        denominator = sum(n for _, n in selected)
        counts = {str(t): sum(totals[c][str(t)] for c, _ in selected) for t in THRESHOLDS}
        result[label] = {'reference_bases': denominator, 'bases_at_least': counts,
                         'percent_at_least': {t: 100 * n / denominator if denominator else None for t, n in counts.items()}}
    return {'groups': result, 'contigs': {c: {'reference_bases': n, 'bases_at_least': totals[c]} for c, n in contigs},
            'scope': 'Full BAM reference dictionary including reference gaps and ambiguous bases; no accessibility mask. MQ >=20; unmapped, secondary, QC-failed, duplicate and supplementary reads excluded. Default mosdepth CIGAR handling and mate-overlap correction; no base-quality filter. Different filters from the BQ-filtered samtools scan. Depth alone does not establish clinical callability.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('bam', 'tool', 'run-directory', 'resource-dir'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args()
    folder = args.run_directory.resolve(strict=True)
    if folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077 or any((p / '.git').exists() for p in [folder, *folder.parents]):
        raise ValueError('Expected an owner-only private analysis directory outside Git')
    os.umask(0o077)
    status = folder / 'depth-state.json'
    def update(state):
        atomic_json(status, {'status': state, 'updated': stamp()})
    lease = ResourceLease('genomics-depth', 4, 4096, directory=args.resource_dir, pids=64)
    try:
        update('waiting_for_capacity')
        while not lease.try_acquire():
            time.sleep(1)
        update('running')
        header = subprocess.check_output(['samtools', 'view', '--no-PG', '-H', str(args.bam)], text=True)
        contigs = []
        for line in header.splitlines():
            if line.startswith('@SQ\t'):
                fields = dict(item.split(':', 1) for item in line.split('\t')[1:])
                contigs.append((fields['SN'], int(fields['LN'])))
        bed = folder / 'depth-windows.bed'
        with bed.open('x') as stream:
            for index, (chrom, start, end) in enumerate(windows(contigs)):
                stream.write(f'{chrom}\t{start}\t{end}\twindow-{index}\n')
        prefix = folder / 'depth'
        command = [str(args.tool), '-n', '-t', '3', '-Q', '20', '-F', '3844', '--by', str(bed), '--thresholds', ','.join(map(str, THRESHOLDS)), str(prefix), str(args.bam)]
        atomic_json(folder / 'depth-provenance.json', {'command': command, 'tool_sha256': hashlib.sha256(args.tool.read_bytes()).hexdigest(), 'version': subprocess.check_output([str(args.tool), '--version'], text=True).strip(), 'bam_bytes': args.bam.stat().st_size, 'bam_mtime_ns': args.bam.stat().st_mtime_ns, 'contigs': len(contigs), 'reference_bases': sum(n for _, n in contigs), 'started': stamp()})
        with (folder / 'depth.log').open('x') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        atomic_json(folder / 'depth-summary.json', summarize(Path(str(prefix) + '.thresholds.bed.gz'), contigs))
        update('passed')
    except Exception:
        update('failed')
        raise
    finally:
        lease.close()


if __name__ == '__main__':
    main()
