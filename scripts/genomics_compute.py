"""Fixed, streaming genomic summary worker. Input is gzip VCF on stdin; no disk I/O."""
from collections import Counter
import gzip
import hashlib
import io
import json
import math
import sys
import time

MAX_PAYLOAD=128*1024*1024


def compute(payload,kind):
    if kind not in ('qc','annotation'):raise ValueError('Unknown analysis')
    result={'kind':kind,'records':0,'records_with_csq':0}
    counters={key:Counter() for key in ['consequences','filters','variant_types','genotypes','depth_bins','quality_bins']}
    sums={'depth_sum':0,'depth_count':0,'quality_sum':0,'quality_count':0,'transitions':0,'transversions':0}
    csq_index=None;samples=None
    with io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(payload)),encoding='utf8') as stream:
        for line in stream:
            if line.startswith('##INFO=<ID=CSQ,'):
                fields=line.split('Format: ',1)[1].split('"',1)[0].split('|');csq_index=fields.index('Consequence')
            if line.startswith('#CHROM\t'):samples=max(0,len(line.rstrip().split('\t'))-9)
            if line.startswith('#'):continue
            fields=line.rstrip('\n').split('\t')
            if len(fields)<8 or samples is None:raise ValueError('Malformed VCF')
            result['records']+=1
            if kind=='annotation':
                if csq_index is None:raise ValueError('Missing consequence schema')
                csq=next((item[4:] for item in fields[7].split(';') if item.startswith('CSQ=')),None)
                if csq and csq!='.':
                    result['records_with_csq']+=1;terms=set()
                    for annotation in csq.split(','):
                        values=annotation.split('|')
                        if len(values)<=csq_index:raise ValueError('Malformed consequence annotation')
                        terms.update(values[csq_index].split('&'))
                    counters['consequences'].update(terms)
                continue
            if samples!=1 or len(fields)!=10:raise ValueError('QC requires exactly one sample')
            counters['filters'][fields[6]]+=1
            ref=fields[3];alts=fields[4].split(',');types=set()
            for alt in alts:
                if alt in ('.','<NON_REF>','<*>'):raise ValueError('QC requires prepared called variants')
                if alt.startswith('<') or '[' in alt or ']' in alt or alt=='*':types.add('symbolic')
                elif len(ref)==len(alt)==1:
                    types.add('SNV')
                    if ref in 'ACGT' and alt in 'ACGT' and ref!=alt:sums['transitions' if ref+alt in ('AG','GA','CT','TC') else 'transversions']+=1
                elif len(ref)!=len(alt):types.add('indel')
                else:types.add('MNV')
            counters['variant_types']['mixed' if len(types)>1 else next(iter(types))]+=1
            values=dict(zip(fields[8].split(':'),fields[9].split(':')))
            gt=values.get('GT','.').replace('|','/').split('/')
            if '.' in gt:genotype='missing_or_partial'
            elif all(a=='0' for a in gt):genotype='hom_reference'
            elif len(set(gt))>1:genotype='heterozygous'
            else:genotype='alternate_haploid' if len(gt)==1 else 'hom_alternate'
            counters['genotypes'][genotype]+=1
            for field,prefix in [('DP','depth'),('GQ','quality')]:
                value=values.get(field,'.')
                if value=='.':counters[prefix+'_bins']['missing']+=1;continue
                number=float(value)
                if not math.isfinite(number) or number<0:raise ValueError('Invalid QC numeric field')
                label='0' if number==0 else '1-9' if number<10 else '10-19' if number<20 else '20-29' if number<30 else '30-59' if number<60 else '60+'
                counters[prefix+'_bins'][label]+=1;sums[prefix+'_sum']+=number;sums[prefix+'_count']+=1
    if samples is None:raise ValueError('Missing VCF columns')
    result.update({key:dict(value) for key,value in counters.items()});result.update(sums)
    return result


def main():
    started=time.monotonic();cpu=time.process_time()
    try:
        kind=sys.argv[1]
        payload=sys.stdin.buffer.read(MAX_PAYLOAD+1)
        if len(payload)>MAX_PAYLOAD:raise ValueError('Payload too large')
        result=compute(payload,kind)
        result.update(sha256=hashlib.sha256(payload).hexdigest(),worker_seconds=time.monotonic()-started,cpu_seconds=time.process_time()-cpu)
        sys.stdout.write(json.dumps(result))
    except Exception:
        # Never put a malformed genomic row, sample name, or exception value in stderr.
        sys.stderr.write('Genomic batch failed validation or computation\n');sys.exit(1)


if __name__=='__main__':main()
