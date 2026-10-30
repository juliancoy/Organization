import subprocess
import pytest
from scripts.wgs_single_copy_download import failure_exit_code
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
    monkeypatch.setattr(swarm,'AgentClient',Client)
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


def test_archive_exhaustion_remains_retryable_but_http_denial_does_not():
    from scripts.wgs_single_copy_worker import retryable_download_error
    from urllib.error import HTTPError
    assert retryable_download_error(TimeoutError())
    assert retryable_download_error(HTTPError('https://archive',503,'unavailable',{},None))
    assert not retryable_download_error(HTTPError('https://archive',403,'denied',{},None))
    assert not retryable_download_error(ValueError('Publisher checksum verification failed'))
