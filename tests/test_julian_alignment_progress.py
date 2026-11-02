import json
from scripts.julian_alignment_progress import snapshot


def test_progress_uses_whole_input_and_redacts_private_data(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'reads':[{'bytes':1000,'path':'/private/person.fastq'}]}))
    (tmp_path/'state.json').write_text(json.dumps({'jobs':{'one':{'phase':'complete','start_offsets':[0],'end_offsets':[100],'pairs':10,'host':'worker.local','private_genotype':'SECRET'},'two':{'phase':'running','start_offsets':[100],'end_offsets':[200],'host':'worker.local'}}}))
    value = snapshot(tmp_path); alignment = next(c for c in value['children'] if c['id']=='alignment')
    assert alignment['completed_units']==100 and alignment['total_units']==1000
    assert alignment['worker']=='worker.local'
    assert '/private/' not in json.dumps(value) and 'SECRET' not in json.dumps(value)


def test_stopped_controller_not_advertised_as_running(tmp_path):
    value = snapshot(tmp_path, False)
    assert value['status']=='interrupted'
    assert not any(c['status']=='running' for c in value['children'])


def test_alignment_eta_excludes_merge_and_uses_completed_bytes(tmp_path, monkeypatch):
    import datetime
    monkeypatch.setattr('scripts.julian_alignment_progress.time.time', lambda: 1000)
    (tmp_path/'manifest.json').write_text(json.dumps({'reads':[{'bytes':10000}]}))
    jobs={str(n):{'phase':'complete','start_offsets':[n*100],'end_offsets':[(n+1)*100], 'pairs':10,
                  'finished':datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat()} for n,t in enumerate([800,850,900])}
    (tmp_path/'state.json').write_text(json.dumps({'jobs':jobs}))
    value=snapshot(tmp_path)
    assert value['primary_progress']['work_percent']==3
    assert value['primary_progress']['units_per_second']==2
    assert value['execution_eta_seconds']==4850
    finalize=next(c for c in value['children'] if c['id']=='finalize')
    assert finalize['status']=='waiting_dependency' and finalize['depends_on']==['alignment']


def test_index_stage_progress_is_not_whole_index_completion(tmp_path):
    (tmp_path/'bwa-index.stderr').write_text('textLength=1000\n500 characters processed\n')
    value=snapshot(tmp_path)
    index=next(c for c in value['children'] if c['id']=='compact-reference')
    assert 'BWT construction: 50.0%' in index['detail']
    assert 'validation follow' in index['detail']


def test_dead_alignment_workers_not_hidden_by_live_gpu_worker(tmp_path):
    (tmp_path/'state.json').write_text(json.dumps({'jobs':{'one':{'phase':'pending'}},'workers':{
        'mapper.local':{'phase':'reconnecting'},'gpu.local':{'phase':'gpu_quality_checks'}}}))
    value=snapshot(tmp_path)
    assert value['status']=='waiting'
    assert 'disconnected' in value['detail']
    assert 'execution_eta_seconds' not in value


def test_owner_pause_and_reference_transfer_are_explicit(tmp_path):
    (tmp_path/'state.json').write_text(json.dumps({'jobs':{},'workers':{
        'audio.local':{'phase':'paused'},'mapper.local':{'phase':'transferring_reference'}}}))
    value=snapshot(tmp_path,True)
    workers={c.get('worker'):c for c in value['children'] if c['id'].startswith('worker-')}
    assert workers['audio.local']['status']=='paused'
    assert 'owner' in workers['audio.local']['detail']
    assert 'Transferring and verifying' in workers['mapper.local']['detail']
