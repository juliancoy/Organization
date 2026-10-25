#!/usr/bin/env python3
"""Build an aggregate-only demo snapshot. Never export variants or sample headers."""
import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

CHROMS=[str(n) for n in range(1,23)]+['X','Y','MT']
SOURCE='https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/rmsk.txt.gz'

def satellite_intervals(path):
    regions=defaultdict(list); families=Counter()
    with gzip.open(path,'rt') as stream:
        for line in stream:
            f=line.rstrip().split('\t')
            if len(f)<17:raise ValueError('Invalid RepeatMasker table')
            chrom=f[5].removeprefix('chr')
            if f[11]=='Satellite' and chrom in CHROMS:
                start,end=int(f[6]),int(f[7]);regions[chrom].append((start,end));families[f[12]]+=end-start
    # Merge to avoid counting overlapping annotations twice.
    merged={}
    for chrom,rows in regions.items():
        out=[]
        for start,end in sorted(rows):
            if out and start<=out[-1][1]:out[-1]=(out[-1][0],max(end,out[-1][1]))
            else:out.append((start,end))
        merged[chrom]=out
    return merged,dict(families)

def distribution(path,regions):
    counts=Counter();bins=defaultdict(Counter);overlap=Counter()
    starts={c:[a for a,b in rows] for c,rows in regions.items()}
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('#'):continue
            f=line.split('\t',5);chrom=f[0].removeprefix('chr')
            if chrom not in CHROMS:continue
            pos=int(f[1])-1;end=pos+len(f[3]);counts[chrom]+=1;bins[chrom][pos//10000000]+=1
            # VCF reference span against 0-based half-open satellite intervals.
            rows=regions.get(chrom,[]);i=bisect.bisect_left(starts.get(chrom,[]),end)-1
            if i>=0 and rows[i][1]>pos:overlap[chrom]+=1
    return dict(counts),{c:dict(rows) for c,rows in bins.items()},dict(overlap)

def build(root,rmsk):
    regions,families=satellite_intervals(rmsk)
    samples=[]
    configs=[('Julian','GRCh38','distributed/run-bppq2zwc','reports/run-ut0ltlcg/called-variants.vcf.gz','VEP 115.2 · existing annotation audit')]
    for name in ['HG001','HG002','HG005','Watson','Venter']:
        configs.append((name,'GRCh37' if name in ('Watson','Venter') else 'GRCh38','public-analysis/run-j_3sudtn',f'public/{name}/variants.vcf.gz','bcftools csq · local consequences'))
    for name,assembly,folder,vcf,engine in configs:
        prefix='' if name=='Julian' else name+'.'
        qc=json.loads((root/folder/(prefix+'qc-summary.json')).read_text())
        annotation=json.loads((root/folder/(prefix+'annotation-summary.json')).read_text())
        chromosomes,bins,overlaps=distribution(root/vcf,regions if assembly=='GRCh38' else {})
        if sum(chromosomes.values())!=qc['records']:raise ValueError('Chromosome count mismatch')
        sample={'name':name,'assembly':assembly,'records':qc['records'],'variant_types':qc['variant_types'],
                'genotypes':qc['genotypes'],'depth_bins':qc['depth_bins'],'quality_bins':qc['quality_bins'],
                'pass':qc['filters'].get('PASS',0),'transitions':qc['transitions'],'transversions':qc['transversions'],
                'consequences':annotation['consequences'],'annotated':annotation['records_with_csq'],
                'chromosomes':chromosomes,'bins':bins,'satellite_overlaps':overlaps if assembly=='GRCh38' else None,'engine':engine}
        samples.append(sample)
    return {'generated_at':datetime.now(timezone.utc).isoformat(),'window_bp':10000000,'samples':samples,
            'satellites':{'source':SOURCE,'sha256':hashlib.sha256(rmsk.read_bytes()).hexdigest(),'assembly':'GRCh38',
            'reference_bp':{c:sum(b-a for a,b in rows) for c,rows in regions.items()},'family_annotation_bp':families}}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench'));parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    data=build(args.root,args.root/'demo/rmsk.txt.gz')
    args.output.write_text('export default '+json.dumps(data,separators=(',',':'))+';\n')
    print('Aggregate snapshot written:',args.output)
if __name__=='__main__':main()
