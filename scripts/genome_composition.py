"""Aggregate variant start positions over primary reference chromosomes.

Unlisted positions are unknown, never inferred reference matches. Insertions
occupy an anchor position, not inserted sequence length. No individual sites
are exported. Mixed alleles at a position are counted as other.
"""
import gzip
import re
from collections import Counter

PRIMARY={str(i) for i in range(1,23)}|{'X','Y','MT'}


def composition(path):
    lengths={}
    positions={}
    with gzip.open(path,'rt') as stream:
        for line in stream:
            if line.startswith('##contig='):
                match=re.search(r'ID=([^,>]+),length=(\d+)',line)
                if match and match[1].removeprefix('chr') in PRIMARY:
                    lengths[match[1].removeprefix('chr')]=int(match[2])
            if line.startswith('#'):
                continue
            fields=line.rstrip('\n').split('\t',5)
            chrom=fields[0].removeprefix('chr')
            if chrom not in PRIMARY:
                continue
            pos=int(fields[1]);ref=fields[3]
            categories=set()
            for alt in fields[4].split(','):
                if not set(ref+alt)<=set('ACGT'):
                    category='other'
                elif len(ref)==len(alt)==1:
                    category='single'
                elif len(alt)>len(ref) and alt.startswith(ref):
                    category='insertions'
                elif len(ref)>len(alt) and ref.startswith(alt):
                    category='deletions'
                else:
                    category='other'
                categories.add(category)
            category=next(iter(categories)) if len(categories)==1 else 'other'
            key=(chrom,pos)
            previous=positions.get(key)
            positions[key]=category if previous in (None,category) else 'other'
    if not lengths or any(c not in lengths or not 1<=p<=lengths[c] for c,p in positions):
        raise ValueError('Missing reference length or out-of-range position')
    counts=Counter(positions.values());total=sum(lengths.values())
    counts['unknown']=total-len(positions)
    return {'reference_positions':total,'counts':dict(counts),'same':None,
            'scope':'Primary reference chromosomes present in the VCF header; variant start positions, not altered-base lengths. Insertions use their reference anchor. Unlisted positions are not confirmed matches.'}
