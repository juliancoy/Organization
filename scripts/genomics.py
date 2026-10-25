#!/usr/bin/env python3
"""Private, read-only genomic inventory and bcftools quality control."""
import argparse
import datetime
from collections import Counter
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

FORMATS={'.vcf':'VCF','.bam':'BAM','.cram':'CRAM','.fq':'FASTQ','.fastq':'FASTQ','.fa':'FASTA','.fasta':'FASTA','.fna':'FASTA','.pgen':'PLINK2','.bed':'PLINK1'}


def file_format(path):
    name=Path(path).name.lower()
    if name.endswith('.gz'):name=name[:-3]
    return FORMATS.get(Path(name).suffix)


def vcf_header(path):
    opener=gzip.open if str(path).endswith('.gz') else open
    result={'samples':0,'references':[],'contigs':0,'annotations':False,'gvcf':False}
    with opener(path,'rt') as stream:
        size=0
        for line in stream:
            size+=len(line)
            if size>16*1024*1024:raise ValueError('VCF header exceeds 16 MiB')
            if line.startswith('##reference='):
                reference=line.rstrip().split('=',1)[1]
                if not reference.startswith(('file:','/')):result['references'].append(reference)
            if line.startswith('##contig='):result['contigs']+=1
            if line.startswith('##INFO=<ID=CSQ,'):result['annotations']=True
            if line.startswith(('##ALT=<ID=NON_REF,','##ALT=<ID=*,','##ALT=<ID=<NON_REF>,')):result['gvcf']=True
            if line.startswith('#CHROM\t'):
                fields=line.rstrip('\n').split('\t')
                if len(fields)<8:raise ValueError('Invalid VCF column header')
                result['samples']=max(0,len(fields)-9)
                return result
            if not line.startswith('#'):break
    raise ValueError('VCF column header is missing')


def inventory(root):
    root=Path(root).resolve(strict=True)
    if not root.is_dir():raise ValueError('Input must be a directory')
    files=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():continue
        kind=file_format(path)
        if not kind:continue
        entry={'path':str(path.relative_to(root)),'format':kind,'bytes':path.stat().st_size}
        if kind=='VCF':
            try:entry['header']=vcf_header(path)
            except (OSError,ValueError,EOFError):entry['header_error']='VCF header could not be validated'
        files.append(entry)
    disk=shutil.disk_usage(root)
    return {'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':str(root),
            'free_bytes':disk.free,'files':files,'note':'Metadata only; no sample identifiers or genotypes are included.'}


def private_run(output):
    output=Path(output).absolute()
    for parent in [output,*output.parents]:
        if (parent/'.git').exists():raise ValueError('Genomic reports must be outside Git workspaces')
        if parent.is_symlink():raise ValueError('Output directories must not be symlinks')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if output.stat().st_uid!=os.getuid():raise ValueError('Output directory must be owned by the current user')
    os.chmod(output,0o700)
    return Path(tempfile.mkdtemp(prefix='run-',dir=output))


def write_json(path,value):
    with path.open('x') as stream:json.dump(value,stream,indent=2)
    path.chmod(0o600)


def quality_control(source,folder,prepared=False):
    source=Path(source).resolve(strict=True)
    if file_format(source)!='VCF':raise ValueError('Quality control requires a VCF or VCF.gz')
    header=vcf_header(source)
    if not header['samples']:raise ValueError('Genotype QC requires sample columns')
    if header['gvcf'] and not prepared:raise ValueError('Use prepare to remove reference blocks and unused alternate alleles first')
    tool=shutil.which('bcftools')
    if not tool:raise RuntimeError('Install bcftools before running QC')
    version=subprocess.check_output([tool,'--version'],text=True).splitlines()[0]
    with (folder/'bcftools.stats.txt').open('x') as report,(folder/'bcftools.stderr.txt').open('x') as errors:
        result=subprocess.run([tool,'stats','-s','-',str(source)],stdout=report,stderr=errors,timeout=3600)
    for name in ['bcftools.stats.txt','bcftools.stderr.txt']:(folder/name).chmod(0o600)
    write_json(folder/'provenance.json',{'tool':version,'source':str(source),'source_bytes':source.stat().st_size,
        'source_mtime_ns':source.stat().st_mtime_ns,'header':header,'exit_code':result.returncode,
        'scope':'Descriptive variant QC only; not clinical interpretation or an association study.'})
    if result.returncode:raise RuntimeError('bcftools failed; inspect the private stderr report')


def called_variants(source,folder):
    source=Path(source).resolve(strict=True)
    if file_format(source)!='VCF':raise ValueError('Expected VCF input')
    if vcf_header(source)['samples']!=1:raise ValueError('This preparation step requires exactly one sample')
    tool=shutil.which('bcftools')
    if not tool:raise RuntimeError('Install bcftools first')
    output=folder/'called-variants.vcf.gz'
    with (folder/'prepare.stderr.txt').open('x') as errors:
        # Retain called alternate genotypes and remove unused alleles. A second
        # stage excludes reference symbols after trimming, preserving real ALTs.
        first=subprocess.Popen([tool,'view','-a','-c','1:nref','-Ou',str(source)],stdout=subprocess.PIPE,stderr=errors)
        try:
            result=subprocess.run([tool,'view','-e','ALT="<NON_REF>" || ALT="<*>" || ALT="."','-Oz','-o',str(output)],stdin=first.stdout,stderr=errors,timeout=3600)
        finally:
            first.stdout.close()
            if first.poll() is None:
                try:first.wait(timeout=10)
                except subprocess.TimeoutExpired:first.kill();first.wait()
        if result.returncode or first.returncode:raise RuntimeError('Variant extraction failed; inspect private stderr')
        subprocess.run([tool,'index',str(output)],stderr=errors,check=True,timeout=300)
    write_json(folder/'preparation.json',{'source':str(source),'source_bytes':source.stat().st_size,
        'scope':'Single-sample called alternate variants only; reference blocks and uncalled genotypes excluded. No PASS/depth/quality threshold has been applied.'})
    return output


def annotation_summary(source,folder):
    source=Path(source).resolve(strict=True)
    if not vcf_header(source)['annotations']:raise ValueError('Input has no VEP CSQ annotation header')
    counts=Counter();records=annotated=0;fields=None
    opener=gzip.open if str(source).endswith('.gz') else open
    with opener(source,'rt') as stream:
        for line in stream:
            if line.startswith('##INFO=<ID=CSQ,'):
                fields=line.split('Format: ',1)[1].split('"',1)[0].split('|')
                continue
            if line.startswith('#'):continue
            records+=1
            info=line.split('\t',8)[7]
            csq=next((value[4:] for value in info.split(';') if value.startswith('CSQ=')),None)
            if not csq or csq=='.':continue
            annotated+=1;consequences=set()
            index=fields.index('Consequence')
            for transcript in csq.split(','):
                values=transcript.split('|')
                if len(values)>index:consequences.update(values[index].split('&'))
            counts.update(consequences)
    write_json(folder/'annotation-summary.json',{'source':str(source),'source_bytes':source.stat().st_size,
        'records':records,'records_with_csq':annotated,'consequence_record_counts':dict(counts),
        'scope':'Existing annotation audit, not a new VEP run. Each consequence counted once per record; categories overlap. Not a pathogenicity classification.'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['inventory','qc','prepare','annotation-summary'])
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,required=True,help='Private directory outside all Git workspaces')
    args=parser.parse_args()
    os.umask(0o077)
    folder=private_run(args.output)
    if args.action=='inventory':write_json(folder/'inventory.json',inventory(args.source))
    elif args.action=='prepare':quality_control(called_variants(args.source,folder),folder,prepared=True)
    elif args.action=='annotation-summary':annotation_summary(args.source,folder)
    else:quality_control(args.source,folder)
    print('Private report saved to '+str(folder))


if __name__=='__main__':main()
