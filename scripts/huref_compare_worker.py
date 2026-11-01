"""Chromosome-scoped assembly alignment and conservative reported-SNP comparison.

Private inputs use pinned container tmpfs pages. Owner-only outputs contain
aggregate counts and hashed site classifications for cross-region deduplication.
Mappings are intrachromosomal research evidence, not validated whole-genome truth.
"""
import bisect
from collections import Counter
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time


def receive(stream,path,size,digest):
    count=0;h=hashlib.sha256()
    with path.open('wb') as out:
        while count<size:
            data=stream.read(min(1024**2,size-count))
            if not data:raise ValueError('Truncated authorized input')
            h.update(data);out.write(data);count+=len(data)
    if h.hexdigest()!=digest:raise ValueError('Input checksum mismatch')


def fasta(path):
    sequence=bytearray()
    with path.open('rb') as f:
        for line in f:
            if line.startswith(b'>'):continue
            sequence.extend(line.strip().upper())
    return sequence


def genotype(fields,min_quality=False):
    values=dict(zip(fields[8].split(':'),fields[9].split(':')))
    gt=values.get('GT','.').replace('|','/').split('/')
    if len(gt)!=2 or any(not a.isdigit() for a in gt):return None
    if min_quality:
        if fields[6]!='PASS':return None
        info=dict(v.split('=',1) for v in fields[7].split(';') if '=' in v)
        depth=values.get('MIN_DP',info.get('MinDP')) if 'END' in info else values.get('DP')
        try:
            quality=[float(depth),float(values.get('GQ','.'))]
            if any(not math.isfinite(v) or v<20 for v in quality):return None
        except (TypeError,ValueError):return None
    alleles=[fields[3],*fields[4].split(',')]
    try:result=[alleles[int(a)] for a in gt]
    except IndexError:return None
    return sorted(result) if all(len(a)==1 and a in 'ACGT' for a in result) else None


def map_snps(paf,candidates,query,reference):
    """Map only substitutions in explicit aligned columns; reject ambiguity."""
    positions=sorted(candidates);mapped={};ambiguous=set();stats=Counter()
    for line in paf:
        fields=line.rstrip().split('\t')
        if len(fields)<12:raise ValueError('Invalid PAF')
        tags={f[:2]:f[5:] for f in fields[12:] if len(f)>=5}
        if int(fields[11])<60 or tags.get('tp')!='P':continue
        base=int(fields[0].removeprefix('tile_')) if fields[0].startswith('tile_') else 0
        if not 0<=base<base+int(fields[1])<=len(query) or int(fields[6])!=len(reference):raise ValueError('Alignment length mismatch')
        cigar=tags.get('cg','');parts=re.findall(r'(\d+)([=XID])',cigar)
        if ''.join(n+op for n,op in parts)!=cigar:raise ValueError('Unsupported alignment CIGAR')
        q=base+int(fields[2]) if fields[4]=='+' else base+int(fields[3])-1;t=int(fields[7]);step=1 if fields[4]=='+' else -1
        for raw,op in parts:
            n=int(raw);stats['alignment_'+op]+=n
            if op in '=X':
                lo,hi=(q,q+n) if step==1 else (q-n+1,q+1)
                for position in positions[bisect.bisect_left(positions,lo):bisect.bisect_left(positions,hi)]:
                    offset=(position-q)*step;target=t+offset
                    if not 0<=target<len(reference):raise ValueError('Mapping outside reference')
                    if position in mapped:ambiguous.add(position)
                    else:mapped[position]=(target,step)
                q+=step*n;t+=n
            elif op=='I':q+=step*n
            else:t+=n
        if t!=int(fields[8]) or (q if step==1 else q+1)!=(base+int(fields[3]) if step==1 else base+int(fields[2])):
            raise ValueError('CIGAR endpoint mismatch')
    for position in ambiguous:mapped.pop(position,None)
    stats['ambiguous_query_sites']=len(ambiguous)
    # Multiple query SNPs mapping to one target are not independent comparisons.
    counts=Counter(target for target,_ in mapped.values())
    mapped={q:v for q,v in mapped.items() if counts[v[0]]==1}
    return mapped,stats


def compare(work,query_start=0,query_end=None):
    query=fasta(work/'query.fa');reference=fasta(work/'reference.fa');candidates={};counts=Counter()
    query_end=len(query) if query_end is None else query_end
    if not 0<=query_start<query_end<=len(query):raise ValueError('Invalid query region')
    with gzip.open(work/'huref.vcf.gz','rt') as stream:
        for line in stream:
            if line.startswith('#'):continue
            f=line.rstrip().split('\t')
            if not query_start<=int(f[1])-1<query_end:continue
            counts['huref_records']+=1
            gt=genotype(f)
            if gt is None or any(len(a)!=1 for a in [f[3],*f[4].split(',')]):continue
            if f[6] not in ('PASS','.'):continue
            q=int(f[1])-1
            if not 0<=q<len(query) or chr(query[q])!=f[3]:raise ValueError('HuRef REF differs from pinned assembly')
            if q in candidates:raise ValueError('Duplicate HuRef SNP position')
            candidates[q]=gt
    with (work/'alignment.paf').open() as stream:mapped,stats=map_snps(stream,candidates,query,reference)
    targets={}
    complement=str.maketrans('ACGT','TGCA')
    for q,(t,step) in mapped.items():targets[t]=sorted(a.translate(complement) for a in candidates[q]) if step==-1 else candidates[q]
    positions=sorted(targets);index=0;site_evidence={t:'unknown' for t in targets}
    with gzip.open(work/'julian.vcf.gz','rt') as stream:
        previous=-1
        for line in stream:
            if line.startswith('#'):continue
            f=line.rstrip().split('\t');start=int(f[1])-1
            if start<previous:raise ValueError('Unsorted Julian chromosome')
            previous=start
            info=dict(v.split('=',1) for v in f[7].split(';') if '=' in v)
            end=int(info.get('END',start+len(f[3])))
            if not 0<=start<end<=len(reference):raise ValueError('Julian interval outside pinned chromosome')
            while index<len(positions) and positions[index]<start:index+=1
            k=index
            while k<len(positions) and positions[k]<end:
                t=positions[k];gt=genotype(f,min_quality=True)
                if t!=start:
                    if gt is not None and gt==[f[3],f[3]] and 'END' in info and f[3]==chr(reference[start]):gt=[chr(reference[t])]*2
                    else:gt=None
                elif f[3]!=chr(reference[t]):gt=None
                if gt is None:counts['julian_not_comparable']+=1
                else:
                    label='genotype_agree' if gt==targets[t] else 'genotype_differ'
                    counts[label]+=1;site_evidence[t]=label
                k+=1
            index=k
    counts['huref_snp_candidates']=len(candidates);counts['mapped_snp_candidates']=len(targets)
    counts['unassessed_candidates']=len(targets)-counts['genotype_agree']-counts['genotype_differ']
    counts.update(stats)
    return {**dict(counts),'site_evidence':site_evidence}


def tile_query(source,destination,limit=10*1024**2,region_start=0,region_end=None):
    """Bound aligner query memory independently of chromosome length."""
    start=region_start;offset=0;pending=bytearray()
    with source.open('rb') as stream,destination.open('wb') as out:
        for line in stream:
            if line.startswith(b'>'):continue
            data=line.strip();end=offset+len(data)
            lo=max(offset,region_start);hi=min(end,region_end if region_end is not None else end)
            if hi>lo:pending.extend(data[lo-offset:hi-offset])
            offset=end
            while len(pending)>=limit:
                out.write(('>tile_'+str(start)+'\n').encode());out.write(pending[:limit]);out.write(b'\n')
                del pending[:limit];start+=limit
            if region_end is not None and offset>=region_end:break
        if pending:out.write(('>tile_'+str(start)+'\n').encode());out.write(pending);out.write(b'\n')


def pin_private_input(path):
    """Keep tmpfs input pages resident without setting a job RAM ceiling."""
    import ctypes,mmap
    with path.open('rb') as source:
        mapping=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_COPY)
    pointer=ctypes.addressof(ctypes.c_char.from_buffer(mapping))
    if ctypes.CDLL(None,use_errno=True).mlock(ctypes.c_void_p(pointer),ctypes.c_size_t(len(mapping)))!=0:
        mapping.close();raise OSError('Private input could not be kept resident')
    return mapping


def progress(phase, **values):
    # Operational measurements only; stderr never includes individual records.
    print(json.dumps({'comparison_progress': True, 'phase': phase, 'sampled_at': time.time(), **values}), file=sys.stderr, flush=True)


def main():
    work=Path('/work');header=json.loads(sys.stdin.buffer.readline())
    progress('receiving inputs')
    receive(sys.stdin.buffer,work/'public.tar.gz',header['public_bytes'],header['public_sha256'])
    receive(sys.stdin.buffer,work/'julian.vcf.gz',header['private_bytes'],header['private_sha256'])
    private_mapping=pin_private_input(work/'julian.vcf.gz')
    with tarfile.open(work/'public.tar.gz','r:gz') as tar:
        seen=set()
        for member in tar:
            if member.name not in {'reference.fa','query.fa','huref.vcf.gz'} or not member.isfile() or member.size>600*1024**2 or member.name in seen:
                raise ValueError('Invalid public bundle member')
            seen.add(member.name)
            with tar.extractfile(member) as source,(work/member.name).open('wb') as dest:
                while data:=source.read(1024**2):dest.write(data)
        if len(seen)!=3:raise ValueError('Incomplete public bundle')
    progress('preparing bounded query tiles')
    tile_query(work/'query.fa',work/'query-tiles.fa',region_start=header.get('query_start',0),region_end=header.get('query_end'))
    (work/'public.tar.gz').unlink()
    progress('aligning query tiles')
    with (work/'alignment.paf').open('w') as out:
        subprocess.run(['minimap2','-x','asm5','--eqx','-c','--secondary=no','-t',str(header['cpus']),
                        '-I','1G','-K','20M',str(work/'reference.fa'),str(work/'query-tiles.fa')],stdout=out,stderr=subprocess.DEVNULL,check=True)
    (work/'query-tiles.fa').unlink()
    progress('validating mapped SNP evidence')
    result=compare(work,header.get('query_start',0),header.get('query_end'))
    # Owner-only site classifications permit cross-shard ambiguity exclusion without exporting genotypes.
    result['site_evidence']={hashlib.sha256((header['public_sha256']+':'+str(t)).encode()).hexdigest():label for t,label in result['site_evidence'].items()}
    result.update(query_start=header.get('query_start',0),query_end=header.get('query_end'),chromosome=header['chromosome'],public_sha256=header['public_sha256'],private_sha256=header['private_sha256'],
                  mapping_scope='Intrachromosomal primary MAPQ60 alignment; genome-wide uniqueness and complex variant equivalence are not established',
                  comparison_scope='Reported HuRef2 diploid SNPs with explicit alleles and Julian PASS DP/GQ20 calls; no genome-wide similarity percentage or missing-as-reference inference')
    private_mapping.close()
    progress('verified')
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('Chromosome comparison failed validation or execution; no private records logged\n');sys.exit(1)
