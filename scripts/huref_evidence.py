"""Keep assembly, local diploid, and mapping evidence distinct in comparisons.

Sequence agreement with one assembled allele is useful evidence even when no
heterozygous VCF record exists. It does not establish a second allele or phase.
Only operational classifications, never alleles, are returned by these helpers.
"""
import bisect
from collections import Counter
import hashlib
import re


def _sequence(value):
    return isinstance(value,str) and bool(value) and not (set(value)-set('ACGT'))


def classify_site(assembly, julian, *, huref=None, haplotypes=None,
                  mapping_valid=True, julian_confident=True):
    result={'assembly':'unknown','diploid':'not_established','evidence':[],
            'uncertainty':{'huref':[], 'julian':[], 'mapping':[]}}
    if not mapping_valid:
        result['uncertainty']['mapping'].append('projection_unvalidated_or_ambiguous')
        return result
    if not _sequence(assembly):
        result['uncertainty']['huref'].append('assembly_base_unresolved')
        return result
    result['evidence'].append('assembly')
    if not julian_confident or julian is None or len(julian)!=2 or not all(_sequence(a) for a in julian):
        result['uncertainty']['julian'].append('confident_diploid_call_unavailable')
        return result
    result['assembly']='supported' if assembly in julian else 'different'
    candidates=[]
    for source,alleles in [('reported_variant',huref),('local_haplotypes',haplotypes)]:
        if alleles is None:continue
        if len(alleles)!=2 or not all(_sequence(a) for a in alleles):
            result['uncertainty']['huref'].append(source+'_unresolved');continue
        result['evidence'].append(source);candidates.append(sorted(alleles))
    if candidates and any(c!=candidates[0] for c in candidates):
        result['uncertainty']['huref'].append('conflicting_diploid_sources')
    elif candidates:
        result['diploid']='agree' if candidates[0]==sorted(julian) else 'differ'
    else:result['uncertainty']['huref'].append('second_allele_not_established_at_site')
    return result


def assembly_site_comparison(paf,query,reference,julian,public_sha256):
    """Compare assembled bases at explicit confident Julian SNV sites.

    PAF must have explicit =/X CIGAR and primary MAPQ60 intrachromosomal
    mappings. Duplicate target mappings are excluded. This measures whether
    an assembled allele occurs in Julian's genotype, not diploid agreement.
    """
    positions=sorted(julian);mapped={};ambiguous=set();complement=str.maketrans('ACGT','TGCA')
    for line in paf:
        fields=line.rstrip().split('\t')
        if len(fields)<12:raise ValueError('Invalid PAF')
        tags={f[:2]:f[5:] for f in fields[12:] if len(f)>=5}
        if int(fields[11])<60 or tags.get('tp')!='P':continue
        base=int(fields[0].removeprefix('tile_')) if fields[0].startswith('tile_') else 0
        if fields[4] not in ('+','-'):raise ValueError('Invalid strand')
        if not 0<=base<base+int(fields[1])<=len(query) or int(fields[6])!=len(reference):raise ValueError('Alignment length mismatch')
        cigar=tags.get('cg','');parts=re.findall(r'(\d+)([=XID])',cigar)
        if not parts or ''.join(n+op for n,op in parts)!=cigar:raise ValueError('Unsupported alignment CIGAR')
        step=1 if fields[4]=='+' else -1
        q=base+int(fields[2]) if step==1 else base+int(fields[3])-1
        t=int(fields[7])
        for raw,op in parts:
            n=int(raw)
            if op in '=X':
                if not 0<=t<t+n<=len(reference):raise ValueError('Alignment outside reference')
                for pos in positions[bisect.bisect_left(positions,t):bisect.bisect_left(positions,t+n)]:
                    query_pos=q+step*(pos-t)
                    if not 0<=query_pos<len(query):raise ValueError('Alignment outside query')
                    allele=chr(query[query_pos])
                    if step==-1:allele=allele.translate(complement)
                    if pos in mapped:ambiguous.add(pos)
                    else:mapped[pos]=classify_site(allele,julian[pos])['assembly']
                q+=step*n;t+=n
            elif op=='I':q+=step*n
            else:t+=n
        if t!=int(fields[8]) or (q if step==1 else q+1)!=(base+int(fields[3]) if step==1 else base+int(fields[2])):raise ValueError('CIGAR endpoint mismatch')
    evidence={hashlib.sha256((public_sha256+':'+str(pos)).encode()).hexdigest():label for pos,label in mapped.items() if pos not in ambiguous}
    return {'counts':dict(Counter(evidence.values())), 'site_evidence':evidence,
            'ambiguous_targets':len(ambiguous),
            'scope':'Assembled HuRef allele membership in explicit Julian PASS DP/GQ20 SNV genotypes; no second HuRef allele, genome-wide confidence or phase inferred.'}
