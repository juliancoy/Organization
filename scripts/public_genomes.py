"""Prepare documented public variant datasets without inventing unavailable calls."""
from collections import Counter
import gzip
import json
import mmap
from pathlib import Path
import re
import subprocess


class Reference:
    def __init__(self,path):
        self.path=Path(path);self.stream=self.path.open('rb');self.data=mmap.mmap(self.stream.fileno(),0,access=mmap.ACCESS_READ)
        self.index={}
        for line in Path(str(path)+'.fai').read_text().splitlines():
            name,length,offset,bases,width=line.split('\t')[:5]
            self.index[name]=tuple(map(int,[length,offset,bases,width]))

    def base(self,chrom,start):
        length,offset,bases,width=self.index[chrom]
        if not 0<=start<length:raise ValueError('Position outside reference')
        return chr(self.data[offset+start//bases*width+start%bases]).upper()

    def sequence(self,chrom,start,end):
        length,offset,bases,width=self.index[chrom]
        if not 0<=start<=end<=length:raise ValueError('Interval outside reference')
        if start==end:return ''
        left=offset+start//bases*width+start%bases
        last=end-1;right=offset+last//bases*width+last%bases+1
        return self.data[left:right].decode('ascii').replace('\n','').replace('\r','').upper()

    def close(self):self.data.close();self.stream.close()


def reference_alias(source,output,assembly):
    source=Path(source).resolve(strict=True);output=Path(output);output.mkdir(exist_ok=True,parents=True,mode=0o700)
    target=output/'reference.fa'
    if not target.exists():target.symlink_to(source)
    elif target.resolve()!=source:raise ValueError('Reference target changed')
    rows=[];aliases={}
    for line in Path(str(source)+'.fai').read_text().splitlines():
        fields=line.split('\t');original=fields[0];name=original
        if assembly=='GRCh37':
            match=re.search(r'CM(\d+)\.1',original)
            if not match:raise ValueError('Unrecognized GRCh37 reference accession')
            number=int(match[1])-662
            name=str(number) if 1<=number<=22 else 'X' if number==23 else 'Y' if number==24 else None
            if not name:raise ValueError('Unexpected GRCh37 chromosome')
        fields[0]=name;aliases[original]=name;rows.append('\t'.join(fields))
    Path(str(target)+'.fai').write_text('\n'.join(rows)+'\n')
    (output/'reference-aliases.json').write_text(json.dumps({'assembly':assembly,'source':str(source),'aliases':aliases},indent=2))
    return target


def convert_pgsnp(source,reference,output,sample):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}',sample):raise ValueError('Invalid public sample label')
    ref=Reference(reference);counts=Counter()
    try:
        with gzip.open(source,'rt')as rows,gzip.open(output,'wt')as dest:
            dest.write('##fileformat=VCFv4.2\n##reference=GRCh37\n##source=UCSC_hg19_pgSnp\n')
            dest.write('##public_data_scope=Observed variant alleles; GT/DP/GQ unknown, not reconstructed from allele counts\n')
            for chrom,(length,*_)in ref.index.items():dest.write(f'##contig=<ID={chrom},length={length}>\n')
            dest.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype unavailable in this import">\n')
            dest.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t'+sample+'\n')
            for line in rows:
                counts['source_records']+=1;fields=line.rstrip().split('\t')
                if len(fields)!=8:raise ValueError('Unexpected UCSC pgSnp columns')
                chrom=fields[1].removeprefix('chr');start,end=int(fields[2]),int(fields[3]);alleles=fields[4].split('/')
                if chrom not in ref.index:counts['excluded_non_primary_or_unavailable_contig']+=1;continue
                if not 0<=end-start<=10000 or any(a!='-'and(not a or any(base not in 'ACGT'for base in a))for a in alleles):counts['excluded_ambiguous_or_unsupported_allele']+=1;continue
                base=ref.sequence(chrom,start,end)
                if any(b not in 'ACGT'for b in base):counts['excluded_reference_unknown']+=1;continue
                alts=sorted({''if a=='-'else a for a in alleles}-{base})
                if not alts:counts['excluded_reference_only']+=1;continue
                position=start+1
                if not base or ''in alts:
                    if start==0:counts['excluded_unanchorable_contig_start']+=1;continue
                    anchor=ref.base(chrom,start-1)
                    if anchor not in 'ACGT':counts['excluded_reference_unknown']+=1;continue
                    base=anchor+base;alts=[anchor+a for a in alts];position=start
                dest.write(f'{chrom}\t{position}\t.\t{base}\t'+','.join(alts)+'\t.\t.\t.\tGT\t./.\n');counts['retained_observed_variant_records']+=1
        Path(str(output)+'.import.json').write_text(json.dumps({'sample':sample,'assembly':'GRCh37','counts':dict(counts),'genotype_inferred':False,'scope':'Observed SNV/indel/MNV alleles; genotypes, DP and GQ remain missing. Ambiguous alleles, unavailable contigs, and unanchorable or >10kb records are excluded and counted.'},indent=2))
    finally:ref.close()
    return dict(counts)


def pipeline(commands,errors):
    processes=[];previous=None
    try:
        for index,command in enumerate(commands):
            process=subprocess.Popen(command,stdin=previous.stdout if previous else None,stdout=subprocess.PIPE if index<len(commands)-1 else subprocess.DEVNULL,stderr=errors)
            if previous:previous.stdout.close()
            previous=process;processes.append(process)
        for process in reversed(processes):
            if process.wait(timeout=1800):raise RuntimeError('Public variant preprocessing failed; inspect dataset logs')
    finally:
        for process in processes:
            if process.poll()is None:process.kill();process.wait()


def prepare(sample,folder,reference,gff):
    folder=Path(folder);source=json.loads((folder/'source.json').read_text());raw=Path(source['path']);variants=folder/'variants.vcf.gz'
    with (folder/'processing.log').open('w')as errors:
        if sample in ['Watson','Venter']:
            converted=folder/'observed-sites.vcf.gz'
            convert_pgsnp(raw,reference,converted,sample)
            pipeline([['bcftools','sort','-Oz','-o',str(variants),str(converted)]],errors)
        else:
            rename=folder/'chromosomes.tsv';rename.write_text(''.join('chr'+c+'\t'+c+'\n'for c in [*map(str,range(1,23)),'X','Y'])+'chrM\tMT\n')
            pipeline([['bcftools','view','-a','-c','1:nref','-Ou',str(raw)],
                      ['bcftools','annotate','--rename-chrs',str(rename),'-Ou'],
                      ['bcftools','norm','-f',str(reference),'-c','e','-d','exact','-Oz','-o',str(variants)]],errors)
        subprocess.run(['bcftools','index','-f',str(variants)],stderr=errors,check=True,timeout=300)
        annotated=folder/'consequences.vcf.gz'
        # Ignore phase and sample dosage deliberately: these are independent,
        # site-local protein-coding consequence predictions, not haplotype inference.
        subprocess.run(['bcftools','csq','--local-csq','--samples','-','--fasta-ref',str(reference),'--gff-annot',str(gff),
                        '--threads','2','-Oz','-o',str(annotated),str(variants)],stderr=errors,stdout=subprocess.DEVNULL,check=True,timeout=3600)
        subprocess.run(['bcftools','index','-f',str(annotated)],stderr=errors,check=True,timeout=300)
    metadata={'sample':sample,'assembly':source['assembly'],'input_sha256':source['sha256'],'tool':subprocess.check_output(['bcftools','--version'],text=True).splitlines()[0],
              'reference':str(reference),'gff':str(gff),'annotation_engine':'bcftools csq --local-csq --samples -','annotation_tag':'BCSQ',
              'scope':'Independent protein-coding consequences. Missing annotations are not evidence of benign effect. No pathogenicity, imputation, or association inference.',
              'variants':str(variants),'annotated':str(annotated)}
    (folder/'analysis-source.json').write_text(json.dumps(metadata,indent=2))
    return variants,annotated


def main():
    import argparse
    import os
    from concurrent.futures import ThreadPoolExecutor
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','analyse']);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--reference37',type=Path);parser.add_argument('--reference38',type=Path)
    parser.add_argument('--output',type=Path);parser.add_argument('--resume',type=Path)
    args=parser.parse_args();os.umask(0o077);root=args.root.resolve(strict=True)
    if any((parent/'.git').exists()for parent in [root,*root.parents]):raise ValueError('Keep public genome data outside Git')
    samples=['Watson','Venter','HG001','HG002','HG005']
    if args.action=='prepare':
        if not args.reference37 or not args.reference38:parser.error('Provide both assembly reference files')
        references={assembly:reference_alias(source,root/assembly,assembly)for assembly,source in [('GRCh37',args.reference37),('GRCh38',args.reference38)]}
        def run(sample):
            assembly='GRCh37'if sample in ['Watson','Venter']else'GRCh38'
            gff=next((root/(assembly+'-GFF')).glob('*.gff3.gz'))
            prepare(sample,root/sample,references[assembly],gff)
            return {'sample':sample,'status':'passed'}
        with ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(run,samples))
        (root/'preparation-status.json').write_text(json.dumps(results,indent=2))
    else:
        if not args.output:parser.error('Provide a private output directory')
        from scripts.genomics import private_run
        from scripts.genomics_distributed import Run,discover
        sources={}
        for sample in samples:
            folder=root/sample;metadata=json.loads((folder/'analysis-source.json').read_text())
            for label,key in [('qc','variants'),('annotation','annotated')]:sources[sample+'.'+label]=Path(metadata[key]).resolve(strict=True)
            if not (folder/'bcftools.stats.txt').exists():
                with (folder/'bcftools.stats.txt').open('x')as stats,(folder/'qc.stderr.txt').open('w')as errors:
                    subprocess.run(['bcftools','stats','-s','-',metadata['variants']],stdout=stats,stderr=errors,check=True,timeout=600)
        if args.resume:
            folder=args.resume.resolve(strict=True)
            if folder.parent!=args.output.resolve()or folder.stat().st_mode&0o077:raise ValueError('Resume requires an owner-only run under output')
        else:folder=private_run(args.output)
        print('Public comparison analysis: '+str(folder),flush=True)
        run=Run(discover(),folder,slots=2,resume=bool(args.resume));run.save();run.run(sources,50000)
        print('Public comparison analysis passed',flush=True)


if __name__=='__main__':main()
