import pytest
from scripts.alignment_phase_timing import parse_log, summarize


LOG='''[M::main::10.000*1.00] loaded/built the index for 25 target sequence(s)
[M::mm_idx_stat::12.000*1.00] distinct minimizers: 100
[M::worker_pipeline::20.000*2.00] mapped 100 sequences
[M::main] Real time: 21.000 sec; CPU: 30 sec
'''


def test_phases_partition_mapper_time_without_counting_overlap_twice():
    row=parse_log(LOG)
    assert row['seconds']=={'reference_setup':12,'mapping_pipeline':8,'mapper_cleanup':1}
    assert sum(row['seconds'].values())==row['total_seconds']
    assert 'sorting overlaps' in row['scope']


def test_partial_logs_do_not_invent_durations():
    assert parse_log('')['current_phase']=='Load reference index'
    row=parse_log(LOG.split('[M::worker_pipeline')[0])
    assert row['current_phase']=='Prepare reference index'
    assert 'seconds' not in row
    assert 'seconds' not in parse_log(LOG.replace('21.000 sec','15.000 sec'))


def test_summary_uses_completed_logs_and_never_exposes_read_names(tmp_path):
    folder=tmp_path/'attempts';folder.mkdir()
    (folder/'node_local-1.stderr').write_text(LOG+'private-read-name SECRET\n')
    row=summarize(tmp_path,[{'id':'1','host':'node.local','phase':'complete','finished':'2026-11-01'}])
    assert row['sample_count']==1 and row['dominant_phase']=='reference_setup'
    assert row['dominant_percent']==pytest.approx(57.1)
    assert 'SECRET' not in str(row)
