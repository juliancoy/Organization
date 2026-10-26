import json
from wgs_progress import snapshot


def test_progress_projects_only_operational_counts(tmp_path):
    folder=tmp_path/'downloads';folder.mkdir()
    (folder/'state.json').write_text(json.dumps({'status':'running','workers':[{'host':'peer','cache':'/secret/path','free_bytes':500}], 'jobs':{'a':{'host':'peer','sample':'Watson','bytes':100,'status':'verified','receipt':{'path':'/private/reads','url':'private'}},'b':{'host':'peer','sample':'Watson','bytes':100,'status':'downloading','progress':{'bytes_completed':40}}}}))
    result=snapshot(tmp_path);sample=result['samples'][0]
    assert sample['files_verified']==1 and sample['files_active']==1 and sample['files_failed']==0
    assert sample['bytes_received']==140 and sample['bytes_total']==200
    assert result['storage']==[{'host':'peer','free_bytes_at_start':500,'assigned_bytes':200}]
    assert 'secret' not in str(result) and 'private' not in str(result)


def test_absent_or_broken_state_is_unavailable(tmp_path):
    assert not snapshot(tmp_path)['available']
    folder=tmp_path/'downloads';folder.mkdir();(folder/'state.json').write_text('partial')
    assert not snapshot(tmp_path)['available']


def test_gpu_qc_exposes_only_aggregate_progress(tmp_path):
    (tmp_path/'gpu-qc').mkdir();(tmp_path/'qc').mkdir()
    (tmp_path/'gpu-qc/service.json').write_text(json.dumps({'status':'running','host':'peer','batches':4,'bases':200,'socket':'/secret/socket','raw_sequence':'private'}))
    (tmp_path/'qc/state.json').write_text(json.dumps({'jobs':{'a':{'gpu_batches':3,'gpu_bases':150,'cpu_bases':20,'quality_histogram':[1]*94}}}))
    value=snapshot(tmp_path)
    assert value['gpu_qc']['batches']==4
    assert value['qc']['gpu_batches']==3 and value['qc']['gpu_bases']==150 and value['qc']['cpu_bases']==20
    assert 'secret' not in str(value) and 'private' not in str(value) and 'quality_histogram' not in str(value)


def test_backfill_is_reported_separately_from_source_qc(tmp_path):
    path=tmp_path/'qc/gpu-replay';path.mkdir(parents=True)
    (path/'state.json').write_text(json.dumps({'status':'running','jobs':{'a':{'phase':'verified','gpu_batches':3,'gpu_bases':42,'gpu_cpu_crosschecked_batches':2,'quality_histogram':[0]*94}}}))
    value=snapshot(tmp_path)
    assert value['qc']['reads_processed']==0 and value['qc']['gpu_bases']==0
    assert value['gpu_backfill']=={'status':'running','files':1,'verified_files':1,'batches':3,'bases':42,'cpu_crosschecked_batches':2}
