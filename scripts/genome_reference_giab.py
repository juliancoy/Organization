"""Aggregate GIAB benchmark reference calls without exporting individual sites.

A benchmark BED represents confident small-variant genotypes only when used with
its matching benchmark VCF. Positions in BED minus every represented variant's
full reference span are benchmark homozygous-reference calls, not evidence for
reference identity outside that benchmark. Insertions exclude their reference
anchor; deletions and multi-base alleles exclude their entire REF spans.

Swarm input: gzip UTF-8 rows tagged B\t<chrom>\t<start>\t<end> or
V\t<original VCF row> or P\t<prepared VCF row>. Both V/P spans are
excluded; only V rows count toward original records. Shard only at chromosome boundaries. Output is aggregate
counts per chromosome, never positions or individual genotypes.
"""
import argparse
from collections import defaultdict
import gzip
import io
import hashlib
import json
from pathlib import Path
import sys

AUTOSOMES={str(i) for i in range(1,23)}
SCOPE=('GIAB v4.2.1 benchmark homozygous-reference positions: union of matching '
       'autosomal confidence BED minus the union of full REF spans in official and prepared VCF variants. '
       'Insertion anchors and complete deletion spans are excluded conservatively. '
       'Outside benchmark regions remains unknown. Not a claim about structural '
       'variation or clinical certainty.')


def merge(rows):
    merged=[]
    for start,end in sorted(rows):
        if start<0 or end<=start:
            raise ValueError('Invalid interval')
        if merged and start<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:
            merged.append((start,end))
    return merged


def intersection_size(left,right):
    i=j=total=0
    while i<len(left) and j<len(right):
        a,b=left[i];c,d=right[j]
        total+=max(0,min(b,d)-max(a,c))
        if b<=d:i+=1
        else:j+=1
    return total


def from_lines(lines):
    beds=defaultdict(list);variants=defaultdict(list);records=0;input_records=0;prepared_records=0
    for line in lines:
        if not line.strip():continue
        tag,_,raw=line.rstrip('\n').partition('\t')
        fields=raw.split('\t')
        if tag not in ('B','V','P') or len(fields)<3:
            raise ValueError('Malformed benchmark worker row')
        if tag=='V':input_records+=1
        if tag=='P':prepared_records+=1
        chrom=fields[0].removeprefix('chr')
        if chrom not in AUTOSOMES:continue
        if tag=='B':
            start,end=int(fields[1]),int(fields[2])
            if start<0 or end<=start:raise ValueError('Invalid BED interval')
            beds[chrom].append((start,end))
        else:
            if len(fields)<8:raise ValueError('Malformed VCF record')
            start=int(fields[1])-1;ref=fields[3]
            if start<0 or not ref or ref=='.':raise ValueError('Invalid VCF interval')
            alts=fields[4].split(',')
            if all(alt in ('.','<NON_REF>','<*>') for alt in alts):continue
            end=start+len(ref)
            info=dict(item.split('=',1) for item in fields[7].split(';') if '=' in item)
            if 'END' in info:end=max(end,int(info['END']))
            variants[chrom].append((start,end))
            if tag=='V':records+=1
    per_chrom={}
    for chrom in sorted(beds,key=int):
        bed=merge(beds[chrom]);excluded=merge(variants[chrom])
        confidence=sum(b-a for a,b in bed)
        variant_span=intersection_size(bed,excluded)
        per_chrom[chrom]={'benchmark_positions':confidence,
                         'excluded_variant_span_positions':variant_span,
                         'same':confidence-variant_span}
    return {'kind':'giab_reference','benchmark_positions':sum(c['benchmark_positions'] for c in per_chrom.values()),
            'excluded_variant_span_positions':sum(c['excluded_variant_span_positions'] for c in per_chrom.values()),
            'same':sum(c['same'] for c in per_chrom.values()),
            'records':input_records,'prepared_records':prepared_records,'variant_records':records,'chromosomes':per_chrom,'scope':SCOPE}


def compute(payload,kind='giab'):
    with io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(payload)),encoding='utf8') as stream:
        result=from_lines(stream)
    result.update(kind=kind,sha256=hashlib.sha256(payload).hexdigest())
    return result


def tagged_lines(bed,vcf,prepared=None):
    opener=gzip.open if str(bed).endswith('.gz') else open
    with opener(bed,'rt') as stream:
        for line in stream:
            if line.strip() and not line.startswith(('#','track','browser')):
                yield 'B\t'+line
    for path,tag in [(vcf,'V'),(prepared,'P')]:
        if path is None:continue
        with gzip.open(path,'rt') if str(path).endswith('.gz') else open(path) as stream:
            for line in stream:
                if not line.startswith('#'):yield tag+'\t'+line


def summarize(bed,vcf,prepared=None):
    return from_lines(tagged_lines(bed,vcf,prepared))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',nargs='?',default='giab')
    parser.add_argument('--bed',type=Path);parser.add_argument('--vcf',type=Path);parser.add_argument('--prepared',type=Path)
    args=parser.parse_args()
    if bool(args.bed)!=bool(args.vcf):parser.error('Provide both BED and VCF')
    try:
        result=summarize(args.bed,args.vcf,args.prepared) if args.bed else compute(sys.stdin.buffer.read(),args.kind)
        print(json.dumps(result,separators=(',',':')))
    except Exception:
        print('GIAB reference worker failed validation',file=sys.stderr);sys.exit(1)


if __name__=='__main__':main()
