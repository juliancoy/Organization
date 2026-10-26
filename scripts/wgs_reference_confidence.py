#!/usr/bin/env python3
"""Retain conservative reference confidence before bcftools drops reference PL.

RCQ is -10 log10(P(non-reference genotype | PL)) under equal genotype priors.
This explicit likelihood model is conservative relative to a strong reference
prior. It is not a clinical guarantee or a substitute for caller GQ on variants.
Mapping/base-quality filtering and depth thresholds remain separate requirements.
"""
import math
import sys


def reference_quality(value):
    try:pl=[float(x) for x in value.split(',')]
    except ValueError:return None
    states=len(pl);alleles=int((math.sqrt(8*states+1)-1)/2)
    if states<3 or alleles*(alleles+1)//2!=states or any(not math.isfinite(x) or x<0 for x in pl):return None
    minimum=min(pl);weights=[10**(-(x-minimum)/10) for x in pl]
    error=sum(weights[1:])/sum(weights)
    return min(999,int(-10*math.log10(error))) if error else 999


def annotate(stream,output):
    for line in stream:
        if line.startswith('##INFO=<ID=RCQ,'):raise ValueError('Input already has RCQ; provenance would be ambiguous')
        if line.startswith('#CHROM'):
            output.write('##INFO=<ID=RCQ,Number=1,Type=Integer,Description="Reference genotype confidence from PL with equal genotype priors; wgs_reference_confidence.py">\n')
            output.write('##LifeTechReferenceConfidence=PL_equal_genotype_priors_v1\n')
        if line.startswith('#'):output.write(line);continue
        fields=line.rstrip('\n').split('\t')
        if len(fields)!=10:raise ValueError('One-sample pileup required')
        keys=fields[8].split(':');values=fields[9].split(':');sample=dict(zip(keys,values))
        quality=reference_quality(sample.get('PL','.'))
        expected=(len(fields[4].split(','))+1)*(len(fields[4].split(','))+2)//2
        if len(sample.get('PL','.').split(','))!=expected:quality=None
        if quality is not None:fields[7]=('' if fields[7]=='.' else fields[7]+';')+'RCQ='+str(quality)
        output.write('\t'.join(fields)+'\n')

if __name__=='__main__':annotate(sys.stdin,sys.stdout)
