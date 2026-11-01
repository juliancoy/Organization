"""Follow-up recipes appended to the pinned comparison reader in isolated workers.

Private records/artifacts return only over the owner-authenticated transport.
Public task observations contain progress and evidence summaries only.
"""
import base64
from collections import Counter


def selected_rows(path,start,end):
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('#'):continue
            row=line.rstrip('\n').split('\t')
            if len(row)<10:raise ValueError('Expected single-sample VCF')
            if start<=int(row[1])-1<end:yield row


def normalized_calls(work,header,check_equivalence=False):
    dataset=header['dataset'];start=header['query_start'];end=header['query_end']
    reference=fasta(work/('query.fa' if dataset=='huref' else 'reference.fa'))
    path=work/('huref.vcf.gz' if dataset=='huref' else 'julian.vcf.gz')
    chrom='reference';records={};filters=set();counts=Counter()
    for row in selected_rows(path,start,end):
        source_alts=row[4].split(',')
        real=[(index+1,alt) for index,alt in enumerate(source_alts) if alt not in ('.','<NON_REF>','<*>','*')]
        if not real:continue
        if any(alt.startswith('<') or '[' in alt or ']' in alt for _,alt in real):
            counts['symbolic_excluded']+=1;continue
        p=int(row[1])-1;ref=row[3].upper()
        if bytes(reference[p:p+len(ref)]).decode()!=ref:raise ValueError('Input REF differs from pinned assembly')
        fields=dict(zip(row[8].split(':'),row[9].split(':')))
        mapping={0:0,**{index:new for new,(index,_) in enumerate(real,1)}}
        original_gt=fields.get('GT','.')
        gt=re.sub(r'\d+',lambda match:str(mapping[int(match[0])]) if int(match[0]) in mapping else '.',original_gt)
        ident='source'+str(len(records));records[ident]=(p,ref,[alt for _,alt in real],gt,row[6])
        filters.update(row[6].split(';'))
        counts['input_variant_records']+=1
    refpath=work/'native-reference.fa'
    with refpath.open('wb') as out:out.write(b'>reference\n');out.write(reference);out.write(b'\n')
    subprocess.run(['samtools','faidx',str(refpath)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    inputpath=work/'variants.vcf'
    with inputpath.open('w') as out:
        out.write('##fileformat=VCFv4.2\n##contig=<ID=reference,length='+str(len(reference))+'>\n')
        out.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Original genotype">\n')
        for value in sorted(filters-{'PASS','.'}):out.write('##FILTER=<ID='+value+',Description="Source filter">\n')
        out.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n')
        # Originals stay immutable; this artifact intentionally carries alleles and GT only.
        for ident,(pos,ref,alts,gt,source_filter) in records.items():
            out.write('\t'.join([chrom,str(pos+1),ident,ref,','.join(alts),'.',source_filter,'.','GT',gt])+'\n')
    output=work/'normalized.vcf'
    command=['bcftools','norm','-m','-any','-c','e','-f',str(refpath),'-Ov',str(inputpath)]
    with output.open('wb') as out:
        subprocess.run(command,stdout=out,stderr=subprocess.DEVNULL,check=True)
    second=work/'normalized-again.vcf'
    with second.open('wb') as out:
        subprocess.run(['bcftools','norm','-m','-any','-c','e','-f',str(refpath),'-Ov',str(output)],stdout=out,stderr=subprocess.DEVNULL,check=True)
    def body(path):return [line for line in path.read_text().splitlines() if not line.startswith('#')]
    if body(output)!=body(second):raise ValueError('Normalization not idempotent')
    for line in body(output):
        row=line.split('\t');pos=int(row[1])-1;ref=row[3];alts=row[4].split(',')
        original_pos,original_ref,original_alts,_,_=records[row[2]]
        left=min(pos,original_pos);right=max(pos+len(ref),original_pos+len(original_ref))
        window=bytes(reference[left:right]).decode()
        expected={window[:original_pos-left]+alt+window[original_pos-left+len(original_ref):] for alt in original_alts}
        for alt in alts:
            actual=window[:pos-left]+alt+window[pos-left+len(ref):]
            if actual not in expected:raise ValueError('Normalized allele changed sequence haplotype')
            counts['haplotype_equivalence_checks']+=1
            counts['sv_sized_alleles']+=int(abs(len(alt)-len(ref))>=50)
        counts['normalized_records']+=1
    artifact=gzip.compress(output.read_bytes(),mtime=0)
    return {'counts':dict(counts),'idempotent':True,'ref_validated':True,
            'native_coordinate_system':dataset,'normalized_sha256':hashlib.sha256(artifact).hexdigest(),
            'normalized_vcf_gz_b64':base64.b64encode(artifact).decode() if not check_equivalence else None,
            'scope':'Native-reference allele normalization and sequence-equivalence validation; split GTs are per-allele views, not new joint genotype truth. Originals remain unchanged.'}


def interval_union(rows):
    merged=[]
    for a,b in sorted(rows):
        if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
        else:merged.append([a,b])
    return merged


def evidence_audit(work,header):
    start=header['query_start'];end=header['query_end'];dataset=header['dataset'];counts=Counter();confident=[];types=Counter()
    path=work/('huref.vcf.gz' if dataset=='huref' else 'julian.vcf.gz')
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('#'):continue
            row=line.rstrip().split('\t');pos=int(row[1])-1
            info=dict(v.split('=',1) for v in row[7].split(';') if '=' in v)
            stop=int(info.get('END',pos+len(row[3])))
            if stop<=start or pos>=end:continue
            counts['records']+=1;fields=dict(zip(row[8].split(':'),row[9].split(':')))
            gt=fields.get('GT','.');counts['phased_records']+=int('|' in gt and '.' not in gt)
            counts['unphased_records']+=int('/' in gt and '.' not in gt)
            counts['phase_set_records']+=int(fields.get('PS','.')!='.')
            counts['gq_present']+=int(fields.get('GQ','.')!='.');counts['dp_present']+=int(fields.get('DP','.')!='.')
            called=genotype(row,min_quality=True)
            if called is not None and called==[row[3],row[3]]:
                confident.append((max(start,pos),min(end,stop)))
            for alt in row[4].split(','):
                if alt in ('.','<NON_REF>','<*>','*'):continue
                kind='symbolic' if alt.startswith('<') or '[' in alt or ']' in alt else 'SNV' if len(alt)==len(row[3])==1 else 'indel' if len(alt)!=len(row[3]) else 'MNV'
                types[kind]+=1
                counts['sv_sized_alleles']+=int(kind=='indel' and abs(len(alt)-len(row[3]))>=50)
    spans=interval_union(confident)
    return {'counts':dict(counts),'variant_types':dict(types),'supported_reference_bases':sum(b-a for a,b in spans),
            'reference_confidence_intervals':spans if dataset=='julian' else [],
            'full_reference_confidence_available':dataset=='julian',
            'limitations':[] if dataset=='julian' else ['HuRef2 input contains heterozygous variant records only; absent calls do not establish callable homozygous-reference positions.']}


def signature_audit(work,header):
    payload=json.loads(gzip.decompress((work/'julian.vcf.gz').read_bytes()))
    envelope=payload['receipt'];value=envelope['value'];raw=base64.b64decode(payload['result_b64'],validate=True)
    if hashlib.sha256(raw).hexdigest()!=value['output_sha256']:raise ValueError('Result checksum mismatch')
    (work/'key.der').write_bytes(bytes.fromhex('302a300506032b6570032100')+bytes.fromhex(header['controller_public_key']))
    (work/'signature.bin').write_bytes(base64.b64decode(envelope['signature'],validate=True))
    (work/'signed.json').write_text(json.dumps(value,sort_keys=True,separators=(',',':')))
    subprocess.run(['openssl','pkeyutl','-verify','-pubin','-keyform','DER','-inkey',str(work/'key.der'),'-sigfile',str(work/'signature.bin'),'-rawin','-in',str(work/'signed.json')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    result=json.loads(raw);sites=result.get('site_evidence',{})
    counts=Counter(sites.values())
    if counts.get('genotype_agree',0)!=result.get('genotype_agree',0) or counts.get('genotype_differ',0)!=result.get('genotype_differ',0):raise ValueError('Per-site evidence and aggregate disagree')
    if value['query_start']!=result['query_start'] or value['query_end']!=result['query_end']:raise ValueError('Query boundary identity mismatch')
    if value['public_sha256']!=result['public_sha256'] or value['private_sha256']!=result['private_sha256']:raise ValueError('Input provenance mismatch')
    return {'signature_verified':True,'result_sha256':value['output_sha256'],'site_count':len(sites),
            'query_start_verified':result['query_start'],'query_end_verified':result['query_end'],'counts_reconciled':True,'source_job':value['job']}


def structural_audit(work,header):
    counts=Counter();kinds=Counter()
    with gzip.open(work/'julian.vcf.gz','rt') as stream:
        for line in stream:
            if line.startswith('#'):continue
            row=line.rstrip().split('\t')
            if row[0].removeprefix('chr')!=header['chromosome']:continue
            counts['records']+=1
            info=dict(v.split('=',1) for v in row[7].split(';') if '=' in v)
            kinds[info.get('SVTYPE','breakend' if '[' in row[4] or ']' in row[4] else 'unspecified')]+=1
    return {'counts':dict(counts),'event_types':dict(kinds),'scope':'Inventory of supplied structural/CNV calls; cross-sample SV equivalence is not inferred.'}


def main():
    work=Path('/work');header=json.loads(sys.stdin.buffer.readline())
    receive(sys.stdin.buffer,work/'public.tar.gz',header['public_bytes'],header['public_sha256'])
    receive(sys.stdin.buffer,work/'julian.vcf.gz',header['private_bytes'],header['private_sha256'])
    private_mapping=pin_private_input(work/'julian.vcf.gz')
    with tarfile.open(work/'public.tar.gz','r:gz') as tar:
        for member in tar:
            if member.name not in {'reference.fa','query.fa','huref.vcf.gz'} or not member.isfile():raise ValueError('Invalid input bundle')
            with tar.extractfile(member) as src,(work/member.name).open('wb') as dest:
                while data:=src.read(1024**2):dest.write(data)
    method=header['method']
    if method in ('normalize','representation'):result=normalized_calls(work,header,method=='representation')
    elif method in ('callability','phase'):result=evidence_audit(work,header)
    elif method=='receipt-audit':result=signature_audit(work,header)
    elif method=='sv-scope':result=structural_audit(work,header)
    else:raise ValueError('Unsupported follow-up recipe')
    private_mapping.close()
    result.update(chromosome=header['chromosome'],query_start=header['query_start'],query_end=header['query_end'],
                  public_sha256=header['public_sha256'],private_sha256=header['private_sha256'],
                  job_spec_sha256=header['job_spec_sha256'],method=method,dataset=header['dataset'])
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('Follow-up failed input, tool, or evidence validation; private details retained only on the owner transport\n');sys.exit(1)
