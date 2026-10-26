#!/usr/bin/env python3
"""Reviewable WGS commands; execute only inside a reserved worker container.

BWA-MEM supports both 454 reads >70 bp and Illumina. The same bcftools
multiallelic caller is used for both; platform error profiles and depth still
make these research estimates, not interchangeable truth sets. Call all covered
sites, retaining GQ/DP evidence: absence from a variant VCF proves no match.
"""
import argparse
import hashlib
import gzip
from collections import Counter
import json
import math
from pathlib import Path
import re
import shlex
import subprocess


def digest(path):
    value=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):value.update(block)
    return value.hexdigest()


def verify_reference(reference,expected):
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Expected reference SHA256 is required')
    if digest(reference)!=expected:raise ValueError('Reference digest mismatch')
    fai=Path(str(reference)+'.fai')
    rows=[line.split('\t') for line in fai.read_text().splitlines()]
    lengths={row[0]:int(row[1]) for row in rows}
    if not lengths or any(n<=0 for n in lengths.values()):raise ValueError('Invalid FASTA index')
    return lengths


def allowed_intervals(lengths,excluded):
    """BED complement; excluded intervals cannot contribute evidence."""
    masks={chrom:[] for chrom in lengths}
    for chrom,start,end in excluded:
        if chrom not in lengths or not 0<=start<end<=lengths[chrom]:raise ValueError('Invalid exclusion interval')
        masks[chrom].append((start,end))
    result=[]
    for chrom,length in lengths.items():
        pos=0
        for start,end in sorted(masks[chrom]):
            if start>pos:result.append((chrom,pos,start))
            pos=max(pos,end)
        if pos<length:result.append((chrom,pos,length))
    return result


def fastq_audit(path,limit=100000):
    """Aggregate pilot QC only; never emit read sequences or identifiers."""
    lengths=Counter();qualities=Counter();count=0
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'rt') as stream:
        while count<limit:
            header=stream.readline()
            if not header:break
            seq=stream.readline().rstrip('\n\r');plus=stream.readline();qual=stream.readline().rstrip('\n\r')
            if not header.startswith('@') or not plus.startswith('+') or not seq or len(seq)!=len(qual):raise ValueError('Invalid FASTQ record')
            lengths[len(seq)]+=1;qualities.update(map(ord,qual));count+=1
    if not count:raise ValueError('Empty FASTQ')
    return {'sampled_reads':count,'minimum_length':min(lengths),'maximum_length':max(lengths),
            'reads_below_70bp':sum(n for length,n in lengths.items() if length<70),
            'quality_ascii_min':min(qualities),'quality_ascii_max':max(qualities),
            'audit_scope':'first records only; not a whole-file integrity check'}


def split_pilot_fastq(source,folder,limit=10000):
    """Keep short 454 reads for BWA-backtrack rather than silently dropping them."""
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    outputs={key:(folder/(key+'.fastq')).open('x') for key in ('short','long')};counts={'short':0,'long':0}
    opener=gzip.open if str(source).endswith('.gz') else open
    try:
        with opener(source,'rt') as stream:
            for _ in range(limit):
                header=stream.readline()
                if not header:break
                seq=stream.readline();plus=stream.readline();qual=stream.readline()
                if not header.startswith('@') or not plus.startswith('+') or len(seq.rstrip())!=len(qual.rstrip()):raise ValueError('Malformed FASTQ')
                key='short' if len(seq.rstrip())<70 else 'long'
                outputs[key].write(header+seq+plus+qual);counts[key]+=1
    finally:
        for stream in outputs.values():stream.close()
        for key in outputs:(folder/(key+'.fastq')).chmod(0o600)
    return counts


def pipeline(commands):return ' | '.join(shlex.join(c) for c in commands)


def alignment(reference,reads,output,sample,run,cpus,memory_mb):
    if len(reads) not in (1,2):raise ValueError('One single or two paired FASTQs required')
    if not re.fullmatch('[A-Za-z0-9_.-]+',sample) or not re.fullmatch('[A-Za-z0-9_.-]+',run):raise ValueError('Unsafe read group')
    if cpus<2 or memory_mb<1024:raise ValueError('Alignment needs two reserved CPUs and at least 1024 MiB')
    # Mapper and sorter overlap: reserve one core for sorting and budget heap
    # after the ~5 GiB human BWA index; caller scheduler must provide headroom.
    if memory_mb<7000:raise ValueError('Human BWA-MEM requires at least 7000 MiB reserved memory')
    sort_mb=max(128,min(1024,(memory_mb-6000)//2))
    rg='@RG\\tID:'+run+'\\tSM:'+sample+'\\tLB:'+sample
    return pipeline([['bwa','mem','-t',str(cpus-1),'-R',rg,str(reference),*map(str,reads)],
                     ['samtools','sort','-@','0','-m',str(sort_mb)+'M','-T',str(output)+'.tmp','-o',str(output),'-']])


def merge_commands(bams,output,cpus,memory_mb,list_file=None):
    if (not bams and not list_file) or cpus<2 or memory_mb<1024:raise ValueError('Invalid merge resources')
    # Fixmate after collating all runs permits duplicate detection across runs.
    merge=['samtools','merge','-u','-b',str(list_file),'-'] if list_file else ['samtools','merge','-u','-','--',*map(str,bams)]
    return pipeline([merge,
        ['samtools','collate','-u','-O','-',str(output)+'.collate'],['samtools','fixmate','-m','-u','-','-'],
        ['samtools','sort','-@','0','-m',str(max(128,memory_mb//4))+'M','-T',str(output)+'.tmp','-'],
        ['samtools','markdup','-@','0','-',''+str(output)]])


def call_commands(reference,bam,chrom,allowed_bed,output,confidence_worker='/work/reference_confidence.py'):
    if not re.fullmatch('[A-Za-z0-9_.-]+',chrom):raise ValueError('Invalid chromosome')
    # -T intersects a BED with -r, and therefore never calls masked sites.
    # No -v: reference calls are retained; no depth clipping hidden at 250.
    return pipeline([['bcftools','mpileup','-Ov','-f',str(reference),'-r',chrom,'-T',str(allowed_bed),
        '-q','20','-Q','20','-d','10000','-a','FORMAT/DP,FORMAT/AD',str(bam)],
        ['python',confidence_worker],['bcftools','call','-m','-f','GQ','-Ob','-o',str(output)]])


def classify(ref,alts,gt,dp,gq,passed=True,min_dp=20,min_gq=20):
    """Classification of a called position; never infer uncovered bases."""
    if not passed or dp is None or gq is None or not math.isfinite(dp) or not math.isfinite(gq) or dp<min_dp or gq<min_gq or not gt or any(a is None for a in gt):return 'unknown'
    if any(not isinstance(a,int) or a<0 or a>len(alts) for a in gt):return 'unknown'
    if all(a==0 for a in gt):return 'confirmed_same'
    try:alleles=[alts[a-1] for a in set(gt) if a]
    except (IndexError,TypeError):return 'unknown'
    if any(a in ('*','.') or a.startswith('<') for a in alleles):return 'other'
    if len(ref)==1 and all(len(a)==1 for a in alleles):return 'snv'
    if all(len(a)>len(ref) and a.startswith(ref) for a in alleles):return 'insertion_anchor'
    if all(len(a)<len(ref) and ref.startswith(a) for a in alleles):return 'deletion_start'
    return 'other'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',required=True);parser.add_argument('--reference-sha256',required=True)
    parser.add_argument('--sample',required=True);parser.add_argument('--run',required=True)
    parser.add_argument('--reads',nargs='+',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--cpus',type=int,required=True);parser.add_argument('--memory-mb',type=int,required=True)
    parser.add_argument('--execute',action='store_true',help='Requires a resource lease held by the invoking worker')
    args=parser.parse_args();verify_reference(args.reference,args.reference_sha256)
    command=alignment(args.reference,args.reads,args.output,args.sample,args.run,args.cpus,args.memory_mb)
    if args.execute:
        output=Path(args.output)
        if output.exists():raise ValueError('Refusing to overwrite alignment')
        partial=Path(str(output)+'.partial.bam')
        command=alignment(args.reference,args.reads,partial,args.sample,args.run,args.cpus,args.memory_mb)
        subprocess.run(['bash','-o','pipefail','-c',command],check=True)
        subprocess.run(['samtools','quickcheck',str(partial)],check=True)
        subprocess.run(['samtools','index',str(partial)],check=True)
        partial.replace(output);Path(str(partial)+'.bai').replace(Path(str(output)+'.bai'))
    else:print(json.dumps({'command':command,'reference_sha256':args.reference_sha256,'status':'plan_only'}))

if __name__=='__main__':main()
