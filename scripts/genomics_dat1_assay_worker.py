"""Experimental DAT1 adVNTR validation. Never equate a no-call with absence."""
import collections
import gzip
import json
import logging
import os
import sys
import warnings


def supported_counts(observations):
    """Require three distinct spanning reads per count; reject partial evidence."""
    names=collections.defaultdict(set)
    for read_id,count,spans in observations:
        if spans and count>0:names[count].add(read_id)
    return sorted(count for count,ids in names.items() for _ in ids if len(ids)>=3)


def slice_cigar(cigar,reference_start,query_start,query_length):
    """Project a query window through M/D operations without falsifying mapping."""
    query_end=query_start+query_length;query=0;reference=reference_start
    out=[];start=None
    for operation,length in cigar:
        if operation==2:
            if query_start<query<query_end:out.append((2,length))
            reference+=length
        elif operation==0:
            left=max(query_start,query);right=min(query_end,query+length)
            if left<right:
                if start is None:start=reference+left-query
                out.append((0,right-left))
            query+=length;reference+=length
        else:raise ValueError('Control supports M/D operations only')
    if start is None or sum(n for op,n in out if op==0)!=query_length:
        raise ValueError('Query window exceeds synthetic alignment')
    return start,out


def main():
    warnings.filterwarnings('ignore')
    logging.disable(logging.CRITICAL)
    value=json.loads(gzip.decompress(sys.stdin.buffer.read(8*1024*1024)))
    os.chdir('/work')
    import pysam
    from advntr.reference_vntr import ReferenceVNTR
    from advntr import settings
    from advntr.models import create_vntrs_database,save_reference_vntr_to_database
    start,end=value['start'],value['end'];context=value['reference_context'];context_start=value['context_start']
    seq='N'*context_start+context
    pattern=seq[start:start+40]
    model=ReferenceVNTR(1,pattern,start,'chr5','SLC6A3','3-prime UTR',estimated_repeats=12,chromosome_sequence=seq)
    # Restrict segmentation to the published interval. The HMM may classify
    # one terminal base as flank; preserve that base in the reconstructed context.
    target=seq[start:end]
    segments=model.find_repeat_segments(target)
    joined=''.join(segments)
    offset=target.find(joined) if joined else -1
    unique=offset>=0 and target.find(joined,offset+1)<0
    normalized_start=start+offset
    normalized_end=normalized_start+len(joined)
    boundary_ok=unique and 0<=offset<=1 and 0<=end-normalized_end<=1
    model.start_point=normalized_start
    model.init_from_xml(segments,seq[normalized_start-500:normalized_start],seq[normalized_end:normalized_end+500])
    result={'tool':'adVNTR 1.5.0','scope':'Experimental spanning-only API wrapper; not a clinical assay','absence_confirmed':False,
            'model_repeat_units':len(segments),'model_repeat_bp':model.get_length(),'expected_interval_bp':end-start,
            'published_start':start,'published_end':end,'model_start':normalized_start,'model_end':normalized_end,
            'boundary_convention':'HMM terminal bases assigned to flank; at most one base per edge',
            'reference_reconstructed':boundary_ok and seq[start:normalized_start]+joined+seq[normalized_end:end]==target,'controls':[]}
    if not boundary_ok or len(segments)!=10 or not result['reference_reconstructed']:
        result.update(status='model_validation_failed',reason='Reference segmentation or boundary reconstruction failed. No private genotype was attempted.')
        print(json.dumps(result));return
    start=normalized_start;end=normalized_end
    db='/work/dat1.db';settings.TRAINED_MODELS_DB=db;create_vntrs_database(db);save_reference_vntr_to_database(model)
    left=model.left_flanking_region[-100:];right=model.right_flanking_region[:100];units=model.repeat_segments
    header={'HD':{'VN':'1.6','SO':'coordinate'},'SQ':[{'SN':'5','LN':181538259}]}
    def bam(name,alleles,short=False):
        records=[]
        for haplotype,allele in enumerate(alleles):
            chosen=units[:5]+units[6:] if allele==9 else units
            sequence=left+''.join(chosen)+right
            for n,offset in enumerate(range(0,len(sequence)-149,10) if short else range(20)):
                read=pysam.AlignedSegment();read.query_name=f'control-{name}-{haplotype}-{n}';read.reference_id=0
                read.mapping_quality=60;read.flag=0
                read.reference_start=start-100
                read.query_sequence=sequence[offset:offset+150] if short else sequence
                read.query_qualities=pysam.qualitystring_to_array('I'*len(read.query_sequence))
                cigar=[(0,100+sum(map(len,units[:5]))),(2,len(units[5])),(0,sum(map(len,units[6:]))+100)] if allele==9 else [(0,len(sequence))]
                if short:
                    read.reference_start,read.cigartuples=slice_cigar(cigar,start-100,offset,150)
                else:read.cigartuples=cigar
                records.append(read)
        path='/work/'+name+'.bam'
        with pysam.AlignmentFile(path,'wb',header=header) as stream:
            for read in sorted(records,key=lambda r:r.reference_start):stream.write(read)
        pysam.index(path);return path
    def call(path,name):
        # The packaged 1.5.0 CLI lacks the older --accuracy_filter flag.
        # Use the author's HMM recruitment and genotype likelihood methods,
        # with an explicit spanning-only wrapper that cannot report lower bounds
        # as diploid repeat counts. This custom wrapper is experimental.
        from advntr.vntr_finder import VNTRFinder
        from advntr.hmm_utils import get_number_of_repeats_in_vpath
        finder=VNTRFinder(model)
        selected=finder.select_illumina_reads(path,[])
        observations=[(read.query_name,get_number_of_repeats_in_vpath(read.vpath),
                       finder.read_flanks_repeats_with_confidence(read.vpath,read.sequence)) for read in selected]
        counts=supported_counts(observations)
        genotype,probability=finder.find_genotype_based_on_observed_repeats(counts)
        return {'case':name,'exit_code':0,'genotype':sorted(genotype) if genotype is not None else None,
                'output_valid':True,'recruited_reads':len(selected),'supported_spanning_reads':len(counts),
                'supported_repeat_counts':dict(collections.Counter(counts)),
                'caller_model_probability':probability,'probability_is_not_clinical_confidence':True}
    for name,alleles in [('long_9_9',[9,9]),('long_9_10',[9,10]),('long_10_10',[10,10])]:
        c=call(bam(name,alleles),name);c['expected']=alleles;c['passed']=c['exit_code']==0 and c['output_valid'] and c['genotype']==alleles;result['controls'].append(c)
    for name,alleles in [('short_9_9',[9,9]),('short_9_10',[9,10]),('short_10_10',[10,10])]:
        c=call(bam(name,alleles,True),name);c['expected']=None;c['known_alleles']=alleles;c['passed']=c['exit_code']==0 and c['output_valid'] and c['genotype'] is None;result['controls'].append(c)
    result['long_controls_passed']=all(c['passed'] for c in result['controls'] if c['case'].startswith('long'))
    result['short_read_no_call_controls_passed']=all(c['passed'] for c in result['controls'] if c['case'].startswith('short'))
    result['matched_read_length_genotypes_resolved']=all(c['genotype']==c['known_alleles'] for c in result['controls'] if c['case'].startswith('short'))
    if not result['long_controls_passed'] or not result['short_read_no_call_controls_passed']:
        result.update(status='validation_failed',private_sample_genotyped=False,reason='Do not interpret private absence: caller failed reference-model or matched-read-length sensitivity controls.')
    else:
        result.update(status='software_controls_passed_short_reads_unresolved',private_sample_genotyped=False,reason='Caller passes spanning controls and safely declines matched 150-bp controls. This method is not validated to resolve Julian’s repeat genotype; no private genotyping attempted.')
    print(json.dumps(result))


if __name__=='__main__':main()
