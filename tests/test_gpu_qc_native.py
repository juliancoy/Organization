import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from scripts import gpu_qc_native as native
from scripts.launcher_agent import ResourceLease


def test_native_work_uses_the_shared_pool_and_releases_its_reservation(tmp_path,monkeypatch):
    identity=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    client=Mock();client.call.side_effect=[{'machine':identity,'ready':False,'worker_policy':{'accepting_jobs':True}},{'cpus':4,'memory_mb':8192,'available_mb':6000}]
    monkeypatch.setattr('scripts.wgs_swarm_download.AgentClient',lambda _:client)
    monkeypatch.setattr(native,'RESOURCE_DIR',tmp_path)
    with native.admission({'local':True,'machine':identity},768,Mock()):
        rows=[json.loads(p.read_text())for p in tmp_path.glob('*.json')]
        assert len(rows)==1 and rows[0]['active'] and rows[0]['memory_mb']==768
        assert ResourceLease.pool_capacity(directory=tmp_path)['group_active_jobs']==0
        assert ResourceLease.pool_capacity(directory=tmp_path,group='gpu-qc-native')['group_active_jobs']==1
    assert not list(tmp_path.glob('*.json'));client.close.assert_called_once()


def test_native_work_rejects_remote_or_changed_identity():
    with pytest.raises(RuntimeError,match='attested'):
        with native.admission({'local':False,'machine':'other'},768,Mock()):pass


def test_paused_worker_does_not_admit_new_native_gpu_jobs(tmp_path,monkeypatch):
    identity=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    client=Mock();client.call.return_value={'machine':identity,'worker_policy':{'accepting_jobs':False}}
    monkeypatch.setattr('scripts.wgs_swarm_download.AgentClient',lambda _:client)
    monkeypatch.setattr(native,'RESOURCE_DIR',tmp_path)
    with pytest.raises(RuntimeError,match='policy'):
        with native.admission({'local':True,'machine':identity},768,Mock()):pass
    assert not list(tmp_path.glob('*.json'));client.close.assert_called_once()


def test_native_stream_preserves_dataset_metadata_and_derived_memory(tmp_path,monkeypatch):
    import socket,threading
    from contextlib import contextmanager
    from scripts import wgs_stream_qc as qc
    root=tmp_path/'gpu-qc';root.mkdir();output=tmp_path/'output';output.mkdir()
    sock=socket.socket(socket.AF_UNIX);sock.bind(str(root/'service.sock'))
    host={'host':'compai.local','cache':str(tmp_path/'cache')};item={'sample':'Venter','run':'run','filename':'reads.gz','bytes':100}
    allocated=[];progress=[]
    @contextmanager
    def admission(host,memory,stop):allocated.append(memory);yield
    def aggregate(folder,item,size,**kwargs):
        assert kwargs['gpu']['verify_first'] is True and kwargs['gpu_parallel'] is True
        kwargs['progress']({'phase':'streaming','gpu_batches':1})
        return {'phase':'verified','gpu_batches':2}
    monkeypatch.setattr(native,'admission',admission);monkeypatch.setattr(qc,'aggregate_qc',aggregate);monkeypatch.setattr(qc,'qc_memory_mb',lambda *a,**k:333)
    try:result=native.run_native(host,item,'source',output,progress.append,threading.Event())
    finally:sock.close()
    assert allocated==[333]
    assert result['sample']=='Venter' and result['host']=='compai.local' and result['memory_mb']==333
    assert progress[0]['sample']=='Venter' and progress[0]['gpu_batches']==1
