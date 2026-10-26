"""RAM-only gVCF reference-call summarizer for trusted swarm workers.

The private intermediate intervals must never be exported in public chart data.
Coordinates are zero-based, half-open. Missing quality is not imputed from QUAL.
"""
from collections import defaultdict
import base64
import gzip
import hashlib
import io
import json
import math
import sys
import time
import zlib

MAX_PAYLOAD = 128 * 1024 * 1024
PRIMARY = {str(i) for i in range(1, 23)} | {'X', 'Y', 'MT'}


def union(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def subtract(intervals, excluded):
    """Subtract merged exclusions without counting covered bases twice."""
    result = []
    i = 0
    excluded = union(excluded)
    for start, end in union(intervals):
        while i < len(excluded) and excluded[i][1] <= start:
            i += 1
        j = i
        while j < len(excluded) and excluded[j][0] < end:
            left, right = excluded[j]
            if left > start:
                result.append([start, min(left, end)])
            start = max(start, right)
            if start >= end:
                break
            j += 1
        if start < end:
            result.append([start, end])
    return result


def number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def compute(payload, kind="qc"):
    if kind != "qc":
        raise ValueError("Unknown reference-call analysis")
    started = time.monotonic()
    cpu_started = time.process_time()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('Compressed batch exceeds limit')
    result = {'records': 0, 'primary_records': 0, 'kind': kind, 'sha256': hashlib.sha256(payload).hexdigest()}
    intervals = {key: defaultdict(list) for key in (
        'confirmed_reference', 'reference_quality_unknown',
        'reference_below_threshold', 'variant_exclusions', 'all_variant_spans')}
    with io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(payload))) as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) != 10:
                raise ValueError('Expected single-sample gVCF')
            result['records'] += 1
            chrom = fields[0].removeprefix('chr')
            if chrom not in PRIMARY:
                continue
            result['primary_records'] += 1
            pos = int(fields[1])
            if pos < 1:
                raise ValueError('Invalid position')
            ref = fields[3]
            info = dict(item.split('=', 1) for item in fields[7].split(';') if '=' in item)
            start, end = pos - 1, max(pos + len(ref) - 1, int(info.get('END', pos)))
            if end <= start:
                raise ValueError('Invalid reference span')
            sample = dict(zip(fields[8].split(':'), fields[9].split(':')))
            gt = sample.get('GT', '.').replace('|', '/').split('/')
            if any(alt not in ('.', '<NON_REF>', '<*>') for alt in fields[4].split(',')):
                intervals['all_variant_spans'][chrom].append([start, end])
            if any(allele not in ('.', '0') for allele in gt):
                intervals['variant_exclusions'][chrom].append([start, end])
                continue
            if not gt or any(allele != '0' for allele in gt) or not set(ref) <= set('ACGT'):
                continue
            # Block DP is not a minimum; only MinDP / MIN_DP can establish it.
            block = 'END' in info and end > pos
            depth = number(info.get('MinDP', sample.get('MIN_DP')))
            if depth is None and not block:
                depth = number(sample.get('DP'))
            quality = number(sample.get('GQ'))
            if fields[6] != 'PASS' or (depth is not None and depth < 20) or (quality is not None and quality < 20):
                category = 'reference_below_threshold'
            elif depth is None or quality is None:
                category = 'reference_quality_unknown'
            else:
                category = 'confirmed_reference'
            intervals[category][chrom].append([start, end])
    merged_intervals = {
        category: {chrom: union(rows) for chrom, rows in by_chrom.items()}
        for category, by_chrom in intervals.items()
    }
    result['intervals_zlib_b64'] = base64.b64encode(zlib.compress(json.dumps(merged_intervals, separators=(',', ':')).encode())).decode()
    result.update(worker_seconds=time.monotonic() - started, cpu_seconds=time.process_time() - cpu_started)
    return result


def intervals_for(part):
    if 'intervals' in part:
        return part['intervals']
    return json.loads(zlib.decompress(base64.b64decode(part['intervals_zlib_b64'])))


def finalize(parts):
    """Combine private worker chunks and subtract variant/conflicting spans."""
    merged = {key: defaultdict(list) for key in (
        'confirmed_reference', 'reference_quality_unknown',
        'reference_below_threshold', 'variant_exclusions', 'all_variant_spans')}
    records = 0
    for part in parts:
        records += part['records']
        for category, by_chrom in intervals_for(part).items():
            for chrom, rows in by_chrom.items():
                merged[category][chrom].extend(rows)
    counts = defaultdict(dict)
    for chrom in PRIMARY:
        exclusions = merged['all_variant_spans'][chrom] + merged['variant_exclusions'][chrom] + merged['reference_below_threshold'][chrom] + merged['reference_quality_unknown'][chrom]
        confirmed = subtract(merged['confirmed_reference'][chrom], exclusions)
        if confirmed:
            counts['confirmed_reference'][chrom] = sum(end - start for start, end in confirmed)
        unqualified = subtract(merged['reference_quality_unknown'][chrom], merged['all_variant_spans'][chrom] + merged['variant_exclusions'][chrom] + merged['reference_below_threshold'][chrom])
        if unqualified:
            counts['reference_quality_unknown'][chrom] = sum(end - start for start, end in unqualified)
    return {'records': records, 'counts': dict(counts),
            'criteria': 'GT fully reference; PASS; GQ >=20; minimum depth >=20 (MinDP/MIN_DP for blocks, DP allowed at individual sites); all conflicting variant or uncertain spans excluded.',
            'scope': 'Reference calls against the source gVCF assembly, not independent base-by-base revalidation; reference identity must be verified separately. Missing quality remains unknown; no individual positions exported.'}


def main():
    try:
        result = compute(sys.stdin.buffer.read(MAX_PAYLOAD + 1), sys.argv[1] if len(sys.argv) > 1 else 'qc')
        sys.stdout.write(json.dumps(result, separators=(',', ':')))
    except Exception:
        sys.stderr.write('Reference-call batch failed validation\n')
        sys.exit(1)


if __name__ == '__main__':
    main()
