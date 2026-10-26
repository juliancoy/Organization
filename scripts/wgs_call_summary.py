#!/usr/bin/env python3
"""Stream one chromosome's single-sample VCF into private evidence artifacts.

Retain confident variant records and reference intervals, never infer equality
from silence. Every recorded ALT's complete REF span excludes reference matches,
even when the variant genotype is uncertain. No noncanonical or masked inference.
"""
from collections import Counter
import argparse
import json
from pathlib import Path
import sys
from scripts.wgs_recall import classify

CATEGORIES=('confirmed_same','snv','insertion_anchor','deletion_start','other')


def summarize(stream,chrom,length,reference_bed,variant_vcf,excluded=False):
    counts=Counter({key:0 for key in CATEGORIES});record_count=0;blocked_until=0
    pending=[];pending_pos=None;bed_start=None;bed_end=None;last_position=-1
    def emit_reference(position):
        nonlocal bed_start,bed_end
        if bed_end==position:bed_end=position+1
        else:
            if bed_start is not None:reference_bed.write(f'{chrom}\t{bed_start}\t{bed_end}\n')
            bed_start=position;bed_end=position+1
        counts['confirmed_same']+=1
    def flush():
        nonlocal blocked_until
        if not pending:return
        pos=pending[0][0]
        for _,fields,category in pending:
            if fields[4]!='.':blocked_until=max(blocked_until,pos+len(fields[3]))
        variants={category for _,_,category in pending if category not in ('unknown','confirmed_same')}
        if variants:
            category=next(iter(variants)) if len(variants)==1 else 'other';counts[category]+=1
            for _,fields,kind in pending:
                if kind not in ('unknown','confirmed_same'):variant_vcf.write('\t'.join(fields)+'\n')
        elif any(kind=='confirmed_same' for _,_,kind in pending) and pos>=blocked_until:emit_reference(pos)
    for line in stream:
        if line.startswith('#'):variant_vcf.write(line);continue
        fields=line.rstrip('\n').split('\t')
        if len(fields)!=10:raise ValueError('Expected one sample')
        if fields[0]!=chrom:raise ValueError('Unexpected chromosome')
        pos=int(fields[1])-1
        if not 0<=pos<length or pos<last_position:raise ValueError('VCF must be sorted within chromosome bounds')
        last_position=pos;record_count+=1
        if excluded:continue
        if pending_pos is not None and pos!=pending_pos:flush();pending=[]
        pending_pos=pos
        values=dict(zip(fields[8].split(':'),fields[9].split(':')))
        def number(key):
            try:return float(values[key])
            except (KeyError,ValueError):return None
        try:gt=tuple(None if a=='.' else int(a) for a in values.get('GT','.').replace('|','/').split('/'))
        except ValueError:gt=()
        reference_gt=bool(gt) and all(a==0 for a in gt)
        quality=number('GQ')
        if reference_gt:
            info=dict(item.split('=',1) for item in fields[7].split(';') if '=' in item)
            try:quality=float(info['RCQ'])
            except (KeyError,ValueError):quality=None
        kind=classify(fields[3],fields[4].split(',') if fields[4]!='.' else [],gt,number('DP'),quality,fields[6] in ('.','PASS'))
        if not set(fields[3])<=set('ACGT'):kind='unknown'
        pending.append((pos,fields,kind))
    flush()
    if bed_start is not None:reference_bed.write(f'{chrom}\t{bed_start}\t{bed_end}\n')
    total=sum(counts.values())
    if total>length:raise ValueError('Category counts exceed chromosome length')
    return {'chrom':chrom,'length':length,'counts':{**counts,'unknown':length-total},'vcf_records':record_count,
        'excluded':excluded,'minimum_DP':20,'minimum_variant_GQ':20,'minimum_reference_RCQ':20,'reference_N':'unknown','reference_quality_model':'Reference RCQ from raw PL with equal genotype priors (v1); variant GQ from bcftools caller',
        'reference_exclusions':'full REF spans of every recorded non-dot ALT; no uncovered inference'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--chrom',required=True);p.add_argument('--length',type=int,required=True);p.add_argument('--output',required=True);p.add_argument('--excluded',action='store_true');args=p.parse_args()
    folder=Path(args.output);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (folder/'confirmed-reference.bed').open('x') as bed,(folder/'variants.vcf').open('x') as vcf:
        result=summarize(sys.stdin,args.chrom,args.length,bed,vcf,args.excluded)
    (folder/'summary.json').write_text(json.dumps(result,indent=2))

if __name__=='__main__':main()
