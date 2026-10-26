"""Curate original Watson and selected HuRef public WGS reads from ENA metadata.

Metadata only: this command never downloads sequence reads. Source file lengths
and MD5 values are publisher-provided, and no arbitrary per-file size cap is used.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,urlopen

FIELDS=('run_accession,study_accession,secondary_study_accession,sample_accession,'
        'experiment_accession,scientific_name,instrument_platform,instrument_model,'
        'library_strategy,library_layout,read_count,base_count,fastq_ftp,fastq_md5,fastq_bytes')
API='https://www.ebi.ac.uk/ena/portal/api/filereport'
WATSON_PROJECT='PRJNA28335'
WATSON_SAMPLE='SAMN00001698'
VENTER_RUN='SRR7097858'


def metadata_url(accession):
    return API+'?'+urlencode({'accession':accession,'result':'read_run','fields':FIELDS,'format':'json'})


def https_file(value):
    value='https://'+value.removeprefix('ftp://').removeprefix('https://')
    parsed=urlsplit(value)
    if parsed.scheme!='https' or parsed.hostname!='ftp.sra.ebi.ac.uk' or not parsed.path.startswith('/vol1/fastq/') or parsed.query or parsed.fragment:
        raise ValueError('Unexpected ENA sequence file location')
    return value


def selected_watson(row):
    return (row.get('study_accession')==WATSON_PROJECT and row.get('sample_accession')==WATSON_SAMPLE
            and row.get('library_strategy')=='WGS' and row.get('instrument_platform')=='LS454'
            and row.get('library_layout')=='SINGLE')


def selected_venter(row):
    return (row.get('run_accession')==VENTER_RUN and row.get('study_accession')=='PRJNA448959'
            and row.get('library_strategy')=='WGS' and row.get('instrument_platform')=='ILLUMINA'
            and row.get('library_layout')=='PAIRED')


def files_for(sample,row):
    locations=row.get('fastq_ftp','').split(';');hashes=row.get('fastq_md5','').split(';');sizes=row.get('fastq_bytes','').split(';')
    if not locations or not all(locations) or len({len(locations),len(hashes),len(sizes)})!=1:
        raise ValueError('Incomplete ENA file identity')
    files=[]
    for location,digest,size in zip(locations,hashes,sizes):
        if not re.fullmatch('[a-fA-F0-9]{32}',digest) or not size.isdigit() or int(size)<1:
            raise ValueError('Invalid ENA checksum or length')
        url=https_file(location);filename=Path(urlsplit(url).path).name
        if not re.fullmatch(re.escape(row['run_accession'])+r'(?:_[12])?\.fastq\.gz',filename):
            raise ValueError('Unexpected ENA sequence file name')
        files.append({'sample':sample,'run':row['run_accession'],'url':url,'bytes':int(size),'md5':digest.lower(),'filename':filename,
                      'source_project':row['study_accession'],'source_sample':row['sample_accession'],
                      'platform':row['instrument_platform'],'instrument_model':row['instrument_model'],
                      'layout':row['library_layout'],'read_count':int(row['read_count']),'base_count':int(row['base_count'])})
    if sample=='Venter' and len(files)!=2:raise ValueError('Selected Venter WGS run must have a FASTQ pair')
    return files


def build(watson,venter):
    selected=[('Watson',row) for row in watson if selected_watson(row)]+[('Venter',row) for row in venter if selected_venter(row)]
    if not any(sample=='Watson' for sample,row in selected) or sum(sample=='Venter' for sample,row in selected)!=1:
        raise ValueError('Expected original Watson WGS and exactly one selected HuRef run')
    files=[entry for sample,row in selected for entry in files_for(sample,row)]
    files.sort(key=lambda row:(row['sample']!='Watson',row['run'],row['filename']))
    identities=[(row['sample'],row['filename']) for row in files]
    if len(identities)!=len(set(identities)):raise ValueError('Repeated source file')
    return {'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'files':files,
            'totals':{'files':len(files),'bytes':sum(row['bytes'] for row in files),'by_sample':{
                sample:{'files':sum(f['sample']==sample for f in files),'runs':sum(s==sample for s,row in selected),
                        'bytes':sum(f['bytes'] for f in files if f['sample']==sample),
                        'bases':sum(int(row['base_count']) for s,row in selected if s==sample)} for sample in ('Watson','Venter')}},
            'selection':{'Watson':'Original 454 WGS only: PRJNA28335, SAMN00001698, LS454, SINGLE. Later CAPILLARY WGA entries in this umbrella project are excluded.',
                         'Venter':'SRR7097858: published 100x, paired-end 2x100bp, 200bp short-insert HuRef Illumina HiSeq 2000 WGS. Exome, mate-pair and linked-read libraries are excluded.'},
            'source_documentation':{'Watson':'https://www.ncbi.nlm.nih.gov/bioproject/PRJNA28335','Venter':'https://www.nature.com/articles/sdata2018261'},
            'scope':'Public WGS read files, not complete gap-free genomes or clinical calls. ENA MD5 values and byte counts identify source files.',
            'watson_release_note':'The original public Watson release deliberately omitted APOE sequence. Do not target or reconstruct intentionally withheld sequence; leave that region excluded from comparison.'}


def fetch(accession):
    url=metadata_url(accession)
    with urlopen(url,timeout=60) as response:raw=response.read()
    rows=json.loads(raw)
    if not isinstance(rows,list):raise ValueError('Invalid ENA metadata response')
    return rows,{'accession':accession,'url':url,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def head_file(file):
    with urlopen(Request(file['url'],method='HEAD'),timeout=40) as response:
        length=response.headers.get('Content-Length')
        if length and int(length)!=file['bytes']:raise ValueError('ENA HTTP length differs from metadata')
        return {'url':file['url'],'status':response.status,'content_length':int(length) if length else None,
                'accept_ranges':response.headers.get('Accept-Ranges'),'checked_at':datetime.now(timezone.utc).isoformat()}


GOLD_ROOT='https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/technical/HuRef_Bina_GoldSet/'
GOLD_FILES=('SNP.vcf.gz','INDEL.vcf.gz','hcref.filtered.bed.gz')


def download_gold_file(folder,name):
    url=GOLD_ROOT+name;target=folder/name;receipt=folder/(name+'.source.json')
    with urlopen(Request(url,method='HEAD'),timeout=40) as response:
        expected=int(response.headers['Content-Length'])
    if shutil.disk_usage(folder).free<expected:raise ValueError('Insufficient local space for gold-set source')
    if not target.exists():
        temporary=target.with_suffix(target.suffix+'.part');size=0
        with urlopen(url,timeout=60) as response,temporary.open('wb') as stream:
            while chunk:=response.read(8*1024*1024):
                stream.write(chunk);size+=len(chunk)
        if size!=expected:raise ValueError('Incomplete HuRef gold-set download')
        temporary.chmod(0o600);temporary.replace(target)
    sha=hashlib.sha256();md5=hashlib.md5();size=0
    with target.open('rb') as stream:
        while chunk:=stream.read(8*1024*1024):sha.update(chunk);md5.update(chunk);size+=len(chunk)
    if size!=expected:raise ValueError('HuRef gold-set HTTP length mismatch')
    if receipt.exists():
        prior=json.loads(receipt.read_text())
        if prior['url']!=url or prior['sha256']!=sha.hexdigest():raise ValueError('Cached HuRef source differs from prior receipt')
    result={'url':url,'filename':name,'bytes':size,'sha256':sha.hexdigest(),'local_md5':md5.hexdigest(),
            'publisher_md5_available':False,'publisher_md5_verified':False,
            'checksum_scope':'Publisher directory provides no checksum manifest; verified HTTPS Content-Length and retained independent local SHA-256/MD5 receipt.',
            'downloaded_at':datetime.now(timezone.utc).isoformat(),'documentation':'https://www.nature.com/articles/srep14493'}
    receipt.write_text(json.dumps(result,indent=2)+'\n');receipt.chmod(0o600)
    return result


def download_gold(folder):
    folder=Path(folder).absolute()
    if any((p/'.git').exists() for p in [folder,*folder.parents]):raise ValueError('Genomic data belongs outside Git')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda name:download_gold_file(folder,name),GOLD_FILES))
    receipt={'dataset':'HuRef Bina Gold Set 2015','files':results,'documentation':'https://www.nature.com/articles/srep14493','scope':'Cross-platform, cross-caller high-confidence small variants and reference regions; assembly must be verified from source headers before comparison.'}
    (folder/'sources.json').write_text(json.dumps(receipt,indent=2)+'\n');(folder/'sources.json').chmod(0o600)
    return receipt


PRIMARY=[str(i) for i in range(1,23)]+['X','Y','MT']


def audit_gold_vcf(path,expected_lengths):
    from collections import Counter
    import gzip
    counts=Counter();filters=Counter();types=Counter();positions=set();lengths={};reference=None;callers=[]
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('##reference='):reference=line.rstrip().split('=',1)[1].rsplit('/',1)[-1]
            if line.startswith('##contig='):
                match=re.search(r'ID=([^,>]+),length=(\d+)',line)
                if match and match[1] in PRIMARY:lengths[match[1]]=int(match[2])
            if line.startswith('#CHROM'):callers=line.rstrip().split('\t')[9:]
            if line.startswith('#'):continue
            f=line.rstrip().split('\t',9);chrom=f[0]
            counts['records']+=1;filters[f[6]]+=1
            if chrom not in PRIMARY:counts['non_primary_records']+=1;continue
            if chrom not in expected_lengths:counts['unavailable_primary_records']+=1;continue
            pos=int(f[1]);ref=f[3];alts=f[4].split(',')
            if not 1<=pos<=expected_lengths[chrom]:raise ValueError('Variant outside expected reference')
            positions.add(PRIMARY.index(chrom)*2**32+pos)
            categories=set()
            for alt in alts:
                if not set(ref+alt)<=set('ACGT'):category='other'
                elif len(ref)==len(alt)==1:category='single'
                elif len(alt)>len(ref) and alt.startswith(ref):category='insertions'
                elif len(ref)>len(alt) and ref.startswith(alt):category='deletions'
                elif len(ref)==len(alt):category='multi'
                else:category='other'
                categories.add(category)
            types[next(iter(categories)) if len(categories)==1 else 'mixed']+=1
    if not lengths or any(expected_lengths[chrom]!=length for chrom,length in lengths.items() if chrom in expected_lengths):raise ValueError('Gold-set assembly length mismatch')
    return {'records':counts['records'],'non_primary_records':counts['non_primary_records'],'unavailable_primary_records':counts['unavailable_primary_records'],'unique_primary_start_positions':len(positions),
            'duplicate_primary_start_records':counts['records']-counts['non_primary_records']-counts['unavailable_primary_records']-len(positions),
            'reference_filename':reference,'primary_contig_lengths':{c:n for c,n in lengths.items() if c in expected_lengths},'source_primary_contigs_not_in_local_reference':[c for c in lengths if c not in expected_lengths],'caller_columns':callers,
            'filters':dict(filters),'variant_types_by_record':dict(types)},positions


def audit_gold(folder,reference_fai):
    import gzip
    folder=Path(folder);expected={f[0]:int(f[1]) for line in Path(reference_fai).read_text().splitlines() if (f:=line.split()) and f[0] in PRIMARY}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda name:audit_gold_vcf(folder/name,expected),('SNP.vcf.gz','INDEL.vcf.gz')))
    snps,indels=results;overlap=len(snps[1]&indels[1]);all_sites=len(snps[1]|indels[1])
    bed_counts={};last_starts={};records=0
    with gzip.open(folder/'hcref.filtered.bed.gz','rt') as stream:
        for line in stream:
            f=line.split();chrom=f[0].removeprefix('chr')
            if chrom not in expected:continue
            start,end=int(f[1]),int(f[2]);records+=1
            if not 0<=start<end<=expected[chrom]:raise ValueError('Reference BED outside assembly')
            if start<last_starts.get(chrom,0):raise ValueError('Source reference BED is not sorted')
            last_starts[chrom]=start
            prior=bed_counts.get(chrom,{'positions':0,'end':0});prior['positions']+=max(0,end-max(start,prior['end']));prior['end']=max(end,prior['end']);bed_counts[chrom]=prior
    result={'assembly':'GRCh37','reference_compatibility':'All locally available primary-contig lengths match the source GRCh37 headers. Source primary contigs absent from the local FASTA are excluded and reported.',
            'sources':{'SNP.vcf.gz':snps[0],'INDEL.vcf.gz':indels[0]},
            'snp_indel_start_position_overlap':overlap,'combined_unique_primary_start_positions':all_sites,
            'high_confidence_reference_bed':{'records':records,'primary_positions':sum(v['positions'] for v in bed_counts.values()),'chromosome_positions':{k:v['positions'] for k,v in bed_counts.items()}},
            'scope':'Source gold-set record audit. Six VCF sample columns are caller/platform replicates of one person. No consensus genotype is inferred. SNP/INDEL overlap positions require union/classification before chart integration. Source reference regions still require subtraction of all chosen variant spans.'}
    (folder/'audit-summary.json').write_text(json.dumps(result,indent=2)+'\n');(folder/'audit-summary.json').chmod(0o600)
    source_receipt=folder/'sources.json'
    if source_receipt.exists():
        receipt=json.loads(source_receipt.read_text());receipt.update(assembly='GRCh37/hs37d5',assembly_audit='audit-summary.json',scope=result['scope'])
        source_receipt.write_text(json.dumps(receipt,indent=2)+'\n');source_receipt.chmod(0o600)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--gold-only',action='store_true');parser.add_argument('--audit-gold',action='store_true');parser.add_argument('--reference-fai',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/public/GRCh37/reference.fa.fai')); parser.add_argument('--gold-output',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/Venter-gold'));parser.add_argument('--output',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/catalog.json'));args=parser.parse_args()
    os.umask(0o077)
    if args.audit_gold:
        print(json.dumps(audit_gold(args.gold_output,args.reference_fai),indent=2));return
    if args.gold_only:
        print(json.dumps(download_gold(args.gold_output),indent=2));return
    output=args.output.absolute()
    if any((p/'.git').exists() for p in [output.parent,*output.parents]):raise ValueError('Genomic manifests belong outside Git')
    os.umask(0o077);output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    watson,wsource=fetch('SRP000095');venter,vsource=fetch(VENTER_RUN)
    catalog=build(watson,venter);catalog['metadata_sources']=[wsource,vsource]
    catalog['availability_checks']=[head_file(next(f for f in catalog['files'] if f['sample']==sample)) for sample in ('Watson','Venter')]
    temporary=output.with_suffix('.tmp');temporary.write_text(json.dumps(catalog,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(output)
    print(json.dumps(catalog['totals'],indent=2))


if __name__=='__main__':main()
