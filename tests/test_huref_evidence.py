"""Swarm-safe acceptance tests: synthetic evidence only; no Docker socket/data.

Assembly agreement, explicit diploid calls and locally resolved haplotypes must
remain distinct. A heterozygous-only VCF never gates assembly comparisons.
"""
import pytest
from scripts.huref_evidence import assembly_site_comparison, classify_site


def paf(strand='+',cigar='8=',quality=60,primary=True):
    return 'query\t8\t0\t8\t'+strand+'\treference\t8\t0\t8\t8\t8\t'+str(quality)+'\ttp:A:'+('P' if primary else 'S')+'\tcg:Z:'+cigar


def test_assembly_evidence_is_useful_without_a_heterozygous_vcf_record():
    value=classify_site('C',['C','T'])
    assert value['assembly']=='supported'
    assert value['diploid']=='not_established'
    assert value['evidence']==['assembly']
    assert value['uncertainty']['julian']==value['uncertainty']['mapping']==[]
    assert value['uncertainty']['huref']==['second_allele_not_established_at_site']


def test_assembly_difference_without_a_vcf_record_is_reported():
    value=classify_site('C',['A','A'])
    assert value['assembly']=='different'
    assert value['diploid']=='not_established'


def test_assembly_membership_does_not_become_diploid_or_phase_agreement():
    value=classify_site('C',['C','T'])
    assert value['diploid']!='agree'
    assert 'phase' not in value


def test_validated_local_haplotypes_supply_diploid_evidence_without_vcf():
    value=classify_site('C',['C','T'],haplotypes=['T','C'])
    assert value['diploid']=='agree'
    assert value['evidence']==['assembly','local_haplotypes']
    assert value['uncertainty']['huref']==[]
    # Haplotype evidence must be provided at each site; it is never carried into
    # an adjacent locus or extrapolated to a chromosome-wide phase claim.
    assert classify_site('C',['C','T'])['diploid']=='not_established'


def test_conflicting_haplotype_and_variant_sources_remain_uncertain():
    value=classify_site('C',['C','T'],huref=['C','T'],haplotypes=['C','C'])
    assert value['diploid']=='not_established'
    assert value['uncertainty']['huref']==['conflicting_diploid_sources']


@pytest.mark.parametrize('called,confident',[(None,True),(['C'],True),(['C','.'],True),(['C','T'],False)])
def test_julian_uncertainty_does_not_get_attributed_to_huref(called,confident):
    value=classify_site('C',called,julian_confident=confident)
    assert value['assembly']=='unknown'
    assert value['uncertainty']['julian']==['confident_diploid_call_unavailable']
    assert value['uncertainty']['huref']==[]


def test_mapping_uncertainty_does_not_get_attributed_to_either_genome():
    value=classify_site('C',['C','T'],mapping_valid=False)
    assert value['uncertainty']['mapping']==['projection_unvalidated_or_ambiguous']
    assert value['uncertainty']['julian']==value['uncertainty']['huref']==[]


def test_unresolved_assembly_sequence_is_huref_uncertainty():
    value=classify_site('N',['C','T'])
    assert value['assembly']=='unknown'
    assert value['uncertainty']['huref']==['assembly_base_unresolved']


def test_real_assembly_comparison_needs_no_variant_vcf():
    result=assembly_site_comparison([paf()],b'ACGTACGT',b'ACGTACGT',{1:['C','T'],4:['C','C']},'public')
    assert result['counts']=={'supported':1,'different':1}
    assert len(result['site_evidence'])==2
    assert all(len(key)==64 for key in result['site_evidence'])


def test_reverse_strand_assembly_bases_are_complemented():
    result=assembly_site_comparison([paf('-')],b'ACGTACGT',b'ACGTACGT',{1:['C','T']},'public')
    assert result['counts']=={'supported':1}


def test_duplicated_mapping_is_excluded_even_when_alleles_agree():
    result=assembly_site_comparison([paf(),paf()],b'ACGTACGT',b'ACGTACGT',{1:['C','T']},'public')
    assert result['counts']=={} and result['ambiguous_targets']==1
    assert result['site_evidence']=={}


@pytest.mark.parametrize('alignment',[paf(quality=59),paf(primary=False)])
def test_mapping_quality_and_primary_alignment_are_required(alignment):
    result=assembly_site_comparison([alignment],b'ACGTACGT',b'ACGTACGT',{1:['C','T']},'public')
    assert result['site_evidence']=={}


def test_deletion_does_not_fabricate_assembled_bases_at_deleted_target():
    result=assembly_site_comparison([paf(cigar='2=1I1D5=')],b'ACGTACGT',b'ACGTACGT',{2:['G','G']},'public')
    assert result['site_evidence']=={}


def test_tile_coordinates_are_resolved_against_full_assembly():
    alignment='tile_4\t4\t0\t4\t+\treference\t8\t4\t8\t4\t4\t60\ttp:A:P\tcg:Z:4='
    result=assembly_site_comparison([alignment],b'ACGTACGT',b'ACGTACGT',{5:['C','T']},'public')
    assert result['counts']=={'supported':1}


def test_malformed_cigar_is_rejected():
    with pytest.raises(ValueError,match='endpoint'):
        assembly_site_comparison([paf(cigar='7=')],b'ACGTACGT',b'ACGTACGT',{1:['C','T']},'public')
