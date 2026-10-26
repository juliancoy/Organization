from scripts.genome_reference_snapshot import apply_reference_matches


def test_reference_counts_partition_without_promoting_unscored_calls():
    snapshot={'samples':[{'name':'Julian','genome_composition':{'reference_positions':100,'counts':{'single':5,'unknown':95},'same':None}}]}
    summaries={'Julian':{'counts':{'confirmed_reference':{'1':20},'reference_quality_unknown':{'1':70}},'workers':['worker-a'],'batches':1}}
    result=apply_reference_matches(snapshot,summaries)
    c=result['samples'][0]['genome_composition']
    assert c['counts']=={'single':5,'same':20,'unknown':75}
    assert c['reference_evidence']['unscored_reference_positions']==70
    assert snapshot['samples'][0]['genome_composition']['same'] is None
    assert apply_reference_matches(result,summaries)['samples']==result['samples']


def test_rejects_mismatched_benchmark_record_count():
    import pytest
    snapshot={'samples':[{'name':'HG001','records':3,'genome_composition':{}}]}
    with pytest.raises(ValueError,match='count differs'):
        apply_reference_matches(snapshot,{'HG001':{'records':4}})


def test_official_and_prepared_counts_are_distinguished():
    snapshot={'samples':[{'name':'HG001','records':2,'genome_composition':{'reference_positions':100,'counts':{'single':2,'unknown':98},'same':None}}]}
    summaries={'HG001':{'records':3,'prepared_records':2,'same':80,'workers':['private-host'],'batches':1,'benchmark_source':{'url':'https://example.org/benchmark.bed'}}}
    c=apply_reference_matches(snapshot,summaries)['samples'][0]['genome_composition']
    assert c['counts']=={'single':2,'same':80,'unknown':18}
    assert c['reference_evidence']['worker_count']==1
    assert 'private-host' not in str(c)
