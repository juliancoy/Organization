"""Regression checks for false genotype/absence claims and control geometry."""
import copy
import pytest
from scripts.genomics_dat1_assay import check_report
from scripts.genomics_dat1_assay_worker import slice_cigar,supported_counts


def test_partial_reads_duplicates_and_under_supported_alleles_do_not_count():
    observations=[('partial'+str(i),10,False) for i in range(100)]
    observations += [('same-read',10,True)]*10
    observations += [('a',9,True),('b',9,True)]
    assert supported_counts(observations)==[]
    observations += [('c',9,True)]
    assert supported_counts(observations)==[9,9,9]


def test_short_control_alignment_preserves_deletion_and_coordinates():
    cigar=[(0,300),(2,40),(0,260)]
    assert slice_cigar(cigar,1000,250,150)==(1250,[(0,50),(2,40),(0,100)])
    assert slice_cigar(cigar,1000,300,150)==(1340,[(0,150)])
    assert slice_cigar(cigar,1000,150,150)==(1150,[(0,150)])
    with pytest.raises(ValueError):slice_cigar(cigar,1000,500,150)


def control_report():
    cases=[]
    for kind in ['long','short']:
        for alleles in [(9,9),(9,10),(10,10)]:
            expected=list(alleles) if kind=='long' else None
            cases.append(dict(case=f'{kind}_{alleles[0]}_{alleles[1]}',expected=expected,
                genotype=expected,exit_code=0,output_valid=True,passed=True))
    return dict(controls=cases,reference_reconstructed=True,model_repeat_units=10,
                absence_confirmed=False,private_sample_genotyped=False,
                status='software_controls_passed_short_reads_unresolved')


def test_public_controls_cannot_claim_a_private_genotype_or_hide_failure():
    report=control_report();assert check_report(report)==report
    for key,value in [('absence_confirmed',True),('private_sample_genotyped',True),('model_repeat_units',9)]:
        broken=copy.deepcopy(report);broken[key]=value
        with pytest.raises(ValueError):check_report(broken)
    for mutation in ['missing','duplicate','wrong_genotype','invalid_output','failed_process']:
        broken=copy.deepcopy(report)
        if mutation=='missing':broken['controls'].pop()
        elif mutation=='duplicate':broken['controls'][-1]=broken['controls'][0]
        elif mutation=='wrong_genotype':broken['controls'][1]['genotype']=[9,9]
        elif mutation=='invalid_output':broken['controls'][3]['output_valid']=False
        else:broken['controls'][3]['exit_code']=2
        with pytest.raises(ValueError):check_report(broken)
