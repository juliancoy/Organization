"""Conservative common-reference comparison of explicitly reported sequence alleles.

Appended to huref_compare_worker functions. Missing calls remain unknown; assembly
projection must be contiguous, unique within the shard, and REF validated.
"""


def sequence_gt(row, quality=False):
    fields=dict(zip(row[8].split(':'),row[9].split(':')))
    raw=fields.get('GT','.').replace('|','/').split('/')
    if len(raw)!=2 or any(not value.isdigit() for value in raw):return None
    alleles=[row[3],*row[4].split(',')]
    try:called=[alleles[int(value)].upper() for value in raw]
    except IndexError:return None
    if any(not value or set(value)-set('ACGT') for value in called):return None
    if quality:
        if row[6]!='PASS':return None
        info=dict(v.split('=',1) for v in row[7].split(';') if '=' in v)
        depth=fields.get('MIN_DP',info.get('MinDP')) if 'END' in info else fields.get('DP')
        try:
            if any(not math.isfinite(float(v)) or float(v)<20 for v in (depth,fields.get('GQ','.'))):return None
        except (TypeError,ValueError):return None
    return sorted(called)


def normalized_rows(work,reference,records):
    """Jointly left normalize both samples, retaining complete diploid alleles."""
    refpath=work/'common.fa'
    refpath.write_bytes(b'>reference\n'+reference+b'\n')
    subprocess.run(['samtools','faidx',str(refpath)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    source=work/'projected.vcf';output=work/'projected.normalized.vcf'
    with source.open('w') as stream:
        stream.write('##fileformat=VCFv4.2\n##contig=<ID=reference,length='+str(len(reference))+'>\n')
        stream.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Source diploid alleles">\n')
        stream.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n')
        for ident,pos,ref,called in records:
            alleles=[ref,*sorted(set(called)-{ref})]
            stream.write('\t'.join(['reference',str(pos+1),ident,ref,','.join(alleles[1:]) or '.', '.', 'PASS', '.', 'GT','/'.join(str(alleles.index(a)) for a in called)])+'\n')
    with output.open('wb') as out:
        subprocess.run(['bcftools','norm','-c','e','-f',str(refpath),'-Ov',str(source)],stdout=out,stderr=subprocess.DEVNULL,check=True)
    result={}
    for line in output.read_text().splitlines():
        if line.startswith('#') or not line.strip():continue
        row=line.split('\t');called=sequence_gt(row)
        if called is None:raise ValueError('Normalization lost explicit genotype')
        result[row[2]]=(int(row[1])-1,row[3],called)
    return result


def projected_compare(work,header):
    query=fasta(work/'query.fa');reference=fasta(work/'reference.fa')
    counts=Counter();candidates={};anchors={}
    start,end=header['query_start'],header['query_end']
    with gzip.open(work/'huref.vcf.gz','rt') as stream:
        for line in stream:
            if line.startswith('#') or not line.strip():continue
            row=line.rstrip().split('\t');pos=int(row[1])-1
            if not start<=pos<end:continue
            counts['reported_records']+=1
            called=sequence_gt(row)
            if called is None or row[6] not in ('PASS','.'):
                counts['excluded_nonsequence_or_filtered']+=1;continue
            ref=row[3].upper()
            if max([len(ref),*[len(a) for a in called]])>10000:
                counts['excluded_over_10kb']+=1;continue
            if bytes(query[pos:pos+len(ref)]).decode()!=ref:raise ValueError('HuRef allele REF mismatch')
            if pos+len(ref)>end:
                counts['boundary_crossing_records']+=1
            ident='h'+str(len(candidates));candidates[ident]=(pos,ref,called)
            for q in range(pos,pos+len(ref)):anchors[q]=True
    with (work/'alignment.paf').open() as stream:mapped,stats=map_snps(stream,anchors,query,reference)
    records=[];metadata={};complement=str.maketrans('ACGT','TGCA')
    for ident,(q,ref,called) in candidates.items():
        first=mapped.get(q)
        if first is None:
            counts['excluded_unmapped_or_ambiguous']+=1;continue
        target,step=first
        if any(mapped.get(q+i)!=(target+step*i,step) for i in range(len(ref))):
            counts['excluded_noncontiguous_projection']+=1;continue
        if step==-1:
            target-=len(ref)-1;ref=ref.translate(complement)[::-1]
            called=sorted(a.translate(complement)[::-1] for a in called)
        if bytes(reference[target:target+len(ref)]).decode()!=ref:
            counts['excluded_common_ref_mismatch']+=1;continue
        kind='SNV' if len(ref)==1 and all(len(a)==1 for a in called) else 'indel' if any(len(a)!=len(ref) for a in called) else 'MNV'
        metadata[ident]=kind;records.append((ident,target,ref,called))
    blocks=[];julian_snvs={};julian_ambiguous=set()
    with gzip.open(work/'julian.vcf.gz','rt') as stream:
        for line in stream:
            if line.startswith('#') or not line.strip():continue
            row=line.rstrip().split('\t');pos=int(row[1])-1;called=sequence_gt(row,True)
            if called is None:continue
            ref=row[3].upper();info=dict(v.split('=',1) for v in row[7].split(';') if '=' in v)
            if bytes(reference[pos:pos+len(ref)]).decode()!=ref:raise ValueError('Julian allele REF mismatch')
            if len(ref)==1 and all(len(a)==1 for a in called) and 'END' not in info:
                if pos in julian_snvs:julian_ambiguous.add(pos)
                else:julian_snvs[pos]=called
            if called==[ref,ref] and 'END' in info:
                stop=int(info['END'])
                if not pos<stop<=len(reference):raise ValueError('Invalid reference confidence block')
                blocks.append((pos,stop));continue
            if max([len(ref),*[len(a) for a in called]])<=10000:
                records.append(('j'+str(len(records)),pos,ref,called))
    for pos in julian_ambiguous:julian_snvs.pop(pos,None)
    with (work/'alignment.paf').open() as stream:
        assembly=assembly_site_comparison(stream,query,reference,julian_snvs,header['public_sha256'])
    normalized=normalized_rows(work,reference,records)
    julian={};ambiguous=set()
    for ident,(pos,ref,called) in normalized.items():
        if not ident.startswith('j'):continue
        if pos in julian:ambiguous.add(pos)
        else:julian[pos]=(ref,called)
    blocks.sort();block_starts=[a for a,b in blocks]
    sites={};site_counts=Counter(pos for ident,(pos,ref,called) in normalized.items() if ident.startswith('h'))
    for ident,(pos,ref,called) in normalized.items():
        if not ident.startswith('h'):continue
        kind=metadata[ident];label='unknown'
        if site_counts[pos]>1 or pos in ambiguous:counts['excluded_duplicate_normalized_position']+=1
        elif pos in julian:
            jref,jcalled=julian[pos]
            if jref==ref:label='agree' if called==jcalled else 'differ'
            else:counts['excluded_incompatible_representation']+=1
        else:
            index=bisect.bisect_right(block_starts,pos)-1
            if index>=0 and blocks[index][0]<=pos and pos+len(ref)<=blocks[index][1]:
                label='agree' if called==[ref,ref] else 'differ'
        counts[kind+'_'+label]+=1
        key=hashlib.sha256((header['public_sha256']+':'+str(pos)).encode()).hexdigest()
        sites[key]={'kind':kind,'label':label}
    return {'counts':dict(counts),'projection_counts':dict(stats),'site_evidence':sites,'assembly_comparison':assembly,
            'scope':'Explicit reported HuRef sequence alleles projected through intrachromosomal primary MAPQ60 contiguous alignment with matching GRCh38 REF; both samples left normalized without splitting diploid genotypes. Julian requires PASS DP/GQ20. Nonprojectable, ambiguous and absent evidence is excluded/unknown; no genome-wide genotype, phase or complex SV equivalence claimed.'}


def main():
    work=Path('/work');header=json.loads(sys.stdin.buffer.readline())
    progress('receiving inputs')
    receive(sys.stdin.buffer,work/'public.tar.gz',header['public_bytes'],header['public_sha256'])
    receive(sys.stdin.buffer,work/'julian.vcf.gz',header['private_bytes'],header['private_sha256'])
    private_mapping=pin_private_input(work/'julian.vcf.gz')
    with tarfile.open(work/'public.tar.gz','r:gz') as tar:
        seen=set()
        for member in tar:
            if member.name not in {'reference.fa','query.fa','huref.vcf.gz'} or not member.isfile() or member.size>600*1024**2 or member.name in seen:raise ValueError('Invalid public bundle')
            seen.add(member.name)
            with tar.extractfile(member) as source,(work/member.name).open('wb') as out:
                while data:=source.read(1024**2):out.write(data)
        if len(seen)!=3:raise ValueError('Incomplete public bundle')
    tile_query(work/'query.fa',work/'query-tiles.fa',region_start=header['query_start'],region_end=header['query_end']+10000)
    (work/'public.tar.gz').unlink()
    progress('aligning common-reference allele projection')
    with (work/'alignment.paf').open('w') as out:
        subprocess.run(['minimap2','-x','asm5','--eqx','-c','--secondary=no','-t',str(header['cpus']),'-I','1G','-K','20M',str(work/'reference.fa'),str(work/'query-tiles.fa')],stdout=out,stderr=subprocess.DEVNULL,check=True)
    (work/'query-tiles.fa').unlink()
    progress('normalizing and comparing reported sequence alleles')
    result=projected_compare(work,header)
    result.update(chromosome=header['chromosome'],query_start=header['query_start'],query_end=header['query_end'],public_sha256=header['public_sha256'],private_sha256=header['private_sha256'],job_spec_sha256=header['job_spec_sha256'])
    private_mapping.close();progress('verified');print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('Projected allele comparison failed validation; private details remain on owner transport\n');sys.exit(1)
