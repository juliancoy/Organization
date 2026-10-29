"""RAM-only DAT1 source callability audit, not a repeat genotype caller."""
import collections
import gzip
import hashlib
import json
import re
import sys


def audit(value):
    locus=value['locus'];start,end=locus['start'],locus['end'];flank=100
    if not (isinstance(start,int) and isinstance(end,int) and 0<start<end and end-start<2000):raise ValueError('Invalid locus')
    coverage=collections.Counter();lengths=collections.Counter();spanning=set();passing=0;total=0
    for line in value['sam'].splitlines():
        if not line or line.startswith('@'):continue
        total+=1;fields=line.split('\t')
        if len(fields)<11:raise ValueError('Invalid alignment record')
        flag,pos,mapq=int(fields[1]),int(fields[3])-1,int(fields[4])
        if fields[2]!=locus['chrom'] or flag&(4|256|512|1024|2048) or mapq<30:continue
        passing+=1;lengths[len(fields[9])]+=1;ref=pos;query=0
        for count,op in re.findall(r'(\d+)([MIDNSHP=X])',fields[5]):
            count=int(count)
            if op in 'M=X':
                for offset in range(max(0,start-flank-ref),min(count,end+flank-ref)):
                    if fields[10]!='*' and ord(fields[10][query+offset])-33>=20:coverage[ref+offset]+=1
                ref+=count;query+=count
            elif op in 'DN':ref+=count
            elif op in 'IS':query+=count
        if pos<=start-20 and ref>=end+20:spanning.add(fields[0])
    def interval(a,b):
        depths=[coverage[p] for p in range(a,b)]
        return {'mean_depth':round(sum(depths)/len(depths),2),'minimum_depth':min(depths),'fraction_at_least_10x':round(sum(d>=10 for d in depths)/len(depths),4)}
    return {'records':total,'high_quality_alignments':passing,'read_lengths':dict(lengths),'direct_spanning_templates':len(spanning),
            'repeat':interval(start,end),'left_flank':interval(start-flank,start),'right_flank':interval(end,end+flank),
            'genotype':'not_assessed','absence_supported':False,
            'scope':'Source-callability audit only. Mapping coverage and missing variant records do not establish repeat count or absence.'}


def main():
    payload=sys.stdin.buffer.read(8*1024*1024+1)
    if len(payload)>8*1024*1024:raise ValueError('Private input bound exceeded')
    value=json.loads(gzip.decompress(payload))
    result=audit(value);result.update(kind='dat1-callability',sha256=hashlib.sha256(payload).hexdigest())
    print(json.dumps(result,separators=(',',':')))


if __name__=='__main__':main()
