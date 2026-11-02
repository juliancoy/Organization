import subprocess
import pytest
from scripts.wgs_single_copy_download import failure_exit_code
from scripts import swarm_runtime
from scripts.genomics_distributed import CapacityWait

@pytest.mark.parametrize('error',[TimeoutError(),ConnectionError(),subprocess.TimeoutExpired('ssh',20),CapacityWait('busy'),RuntimeError('Download lease renewal failed')])
def test_temporary_failures_restart(error):
    assert failure_exit_code(error)==75

@pytest.mark.parametrize('error',[ValueError('checksum mismatch'),RuntimeError('Worker identity/policy changed'),RuntimeError('Worker omitted download receipts'),PermissionError('denied')])
def test_integrity_identity_and_auth_fail_closed(error):
    assert failure_exit_code(error)==78


def test_finalizer_retries_transport_but_not_corrupt_evidence():
    from scripts.wgs_finalize import retryable_stage_failure
    assert retryable_stage_failure(CapacityWait('busy'))
    assert retryable_stage_failure(ConnectionError('peer disconnected'))
    assert retryable_stage_failure(RuntimeError('Container cleanup unconfirmed; resource lease retained'))
    assert not retryable_stage_failure(ValueError('Completed paired shard changed'))
    assert not retryable_stage_failure(PermissionError('denied'))
    assert not retryable_stage_failure(RuntimeError('Recovered chromosome evidence identity mismatch'))

@pytest.mark.parametrize('corrupt_prefix',[False,True])
def test_resumed_lan_copy_requires_full_sha256(tmp_path,monkeypatch,corrupt_prefix):
    import hashlib
    from scripts import wgs_recall_swarm as swarm
    class Client:
        def __init__(self,host):pass
        def call(self,*args,**kwargs):return {'machine':'source-machine'}
        def close(self):pass
    monkeypatch.setattr(swarm_runtime,'AgentClient',Client)
    data=b'verified public reads'*1000;source_file=tmp_path/'source.gz';source_file.write_bytes(data)
    digest=hashlib.sha256(data).hexdigest()
    source={'host':'source','local':True,'machine':'source-machine'}
    target={'host':'target','local':True,'cache':str(tmp_path/'target')}
    final=tmp_path/'target/incoming'/digest/'source.gz';final.parent.mkdir(parents=True)
    partial=final.with_suffix('.gz.partial');partial.write_bytes(b'x'*123 if corrupt_prefix else data[:123])
    receipt={'path':str(source_file),'bytes':len(data),'sha256':digest}
    if corrupt_prefix:
        with pytest.raises(RuntimeError,match='SHA256 mismatch'):swarm.copy_receipt(source,target,receipt,resume=True)
        assert not final.exists()
        assert list(final.parent.glob('*.mismatch-*'))
    else:
        result=swarm.copy_receipt(source,target,receipt,resume=True)
        assert result['path']==str(final)
        assert final.read_bytes()==data
        assert not partial.exists()


def test_incomplete_lan_transfer_retains_prefix_and_resumes_without_wall_timeout(tmp_path,monkeypatch):
    import hashlib,subprocess
    from scripts import wgs_recall_swarm as swarm
    class Client:
        def __init__(self,host):pass
        def call(self,*args,**kwargs):return {'machine':'source-machine'}
        def close(self):pass
    monkeypatch.setattr(swarm_runtime,'AgentClient',Client)
    data=b'verified public reads'*1000;source_file=tmp_path/'source.gz';source_file.write_bytes(data[:123])
    digest=hashlib.sha256(data).hexdigest()
    source={'host':'source','local':True,'machine':'source-machine'}
    target={'host':'target','local':True,'cache':str(tmp_path/'target')}
    receipt={'path':str(source_file),'bytes':len(data),'sha256':digest}
    original_popen=subprocess.Popen;waits=[]
    def popen(*args,**kwargs):
        p=original_popen(*args,**kwargs)
        if kwargs.get('stdout')==subprocess.DEVNULL and kwargs.get('stdin') not in (None,subprocess.DEVNULL):
            communicate=p.communicate
            def wait(*a,**kw):
                waits.append(kw);return communicate(*a,**kw)
            p.communicate=wait
        return p
    monkeypatch.setattr(swarm.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError,match='partial retained'):swarm.copy_receipt(source,target,receipt,resume=True)
    partial=tmp_path/'target/incoming'/digest/'source.gz.partial'
    assert partial.read_bytes()==data[:123]
    assert not list(partial.parent.glob('*.mismatch-*'))
    source_file.write_bytes(data)
    swarm.copy_receipt(source,target,receipt,resume=True)
    assert (tmp_path/'target/incoming'/digest/'source.gz').read_bytes()==data
    assert all('timeout' not in wait for wait in waits)


def test_archive_exhaustion_remains_retryable_but_http_denial_does_not():
    from scripts.wgs_single_copy_worker import retryable_download_error
    from urllib.error import HTTPError
    assert retryable_download_error(TimeoutError())
    assert retryable_download_error(HTTPError('https://archive',503,'unavailable',{},None))
    assert not retryable_download_error(HTTPError('https://archive',403,'denied',{},None))
    assert not retryable_download_error(ValueError('Publisher checksum verification failed'))


def test_cleanup_error_does_not_hide_retryable_transfer_failure(monkeypatch):
    import io,json
    from scripts import wgs_swarm_download as download
    class Client:
        def __init__(self,host):pass
        def call(self,op,*args,**kwargs):
            if op=='status':return {'ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'machine':'peer','capabilities':['renewable-resources']}
            if op=='resource.capacity':return {'available_mb':2048,'available_cpus':4}
            return {'acquired':True}
        def close(self):pass
    item={'url':'https://archive.test/reads'};ident=download.file_id(item)
    class Process:
        returncode=1
        def __init__(self,*args,**kwargs):
            self.stdin=io.BytesIO();self.stdout=io.BytesIO((json.dumps({'id':ident,'error':'TimeoutError','retryable':True})+'\n').encode());self.stderr=io.BytesIO(b'')
        def wait(self):return 1
        def poll(self):return 1
    monkeypatch.setattr(download,'AgentClient',Client)
    monkeypatch.setattr(download.subprocess,'Popen',Process)
    def cleanup(*args):raise RuntimeError('Owned WGS container cleanup unconfirmed; lease retained for server expiry')
    monkeypatch.setattr(download,'release_after_cleanup',cleanup)
    host={'host':'peer','local':True,'machine':'peer','cpus':4,'cache':'/unused','image':'approved','container_user':'0:0'}
    with pytest.raises(OSError,match='Temporary archive transfer failure'):
        download.run_batch(host,[item],lambda *args:None,8,memory_floor_mb=1024)
