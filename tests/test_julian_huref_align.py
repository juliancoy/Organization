import io
import gzip
import pytest
from scripts.julian_huref_align import make_batch, quality_estimate, validate_counts


def read(name, mate, quality=b'III'):
    return b'@' + name + b'/' + str(mate).encode() + b'\nACG\n+\n' + quality + b'\n'


def test_batches_preserve_pairs_and_resume_offsets(tmp_path):
    streams = [io.BytesIO(read(b'a', m) + read(b'b', m)) for m in (1, 2)]
    first = make_batch(streams, tmp_path, 0, 1)
    second = make_batch(streams, tmp_path, 1, 1)
    assert second['start_offsets'] == first['end_offsets']
    assert sum(first['quality_histogram']) == 6
    assert gzip.decompress(open(first['files'][1]['path'], 'rb').read()) == read(b'a', 2)
    assert make_batch(streams, tmp_path, 2, 1) is None


def test_mismatched_mates_do_not_publish(tmp_path):
    with pytest.raises(ValueError, match='Mate identity'):
        make_batch([io.BytesIO(read(b'a', 1)), io.BytesIO(read(b'b', 2))], tmp_path, 0, 1)
    assert not list(tmp_path.iterdir())


def test_truncated_mate_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_batch([io.BytesIO(read(b'a', 1)), io.BytesIO(b'')], tmp_path, 0, 1)


def test_phred_expected_error_not_average_phred():
    hist = [0] * 94; hist[20] = 1; hist[40] = 1
    assert quality_estimate(hist)['predicted_error_fraction'] == pytest.approx(.00505)


def test_primary_accounting_includes_failed_qc():
    validate_counts({'QC-passed reads': {'primary': 3}, 'QC-failed reads': {'primary': 1}}, 2)
    with pytest.raises(ValueError):
        validate_counts({'QC-passed reads': {'primary': 3}, 'QC-failed reads': {'primary': 0}}, 2)


def test_worker_reconnects_with_refreshed_authenticated_endpoint():
    from scripts.julian_huref_align import supervise_worker
    class Stop:
        def is_set(self): return False
        def wait(self, delay): waits.append(delay)
    calls=[]; waits=[]; published=[]; completed=[]
    def discover(required, names):
        calls.append(names)
        if len(calls)==1: return [{'host':'worker.local','eligible':False,'reason':'offline'}]
        return [{'host':'worker.local','eligible':True,'agent_endpoint':'new-'+str(len(calls))}]
    def run(host):
        if len(calls)==2: raise ConnectionRefusedError('disconnected during capacity query')
        completed.append(host['agent_endpoint'])
    supervise_worker('worker.local',run,discover,Stop(),published.append,lambda:bool(completed))
    assert completed==['new-3']
    assert waits==[5,10]
    assert all(names=={'worker.local'} for names in calls)
    assert published[-1]['phase']=='preparing'


def test_reconnect_wait_is_interruptible():
    from scripts.julian_huref_align import supervise_worker
    import threading
    stop=threading.Event(); calls=[]
    def publish(value):
        calls.append(value)
        if value.get('retry_seconds'): stop.set()
    supervise_worker('worker.local',lambda h:None,lambda *a,**k:[],stop,publish,lambda:False)
    assert calls[-1]['phase']=='reconnecting'
    assert calls[-1]['retry_seconds']==5


def test_network_failures_distinct_from_alignment_integrity_failures():
    from scripts.julian_huref_align import connection_failure
    assert connection_failure(ConnectionRefusedError())
    assert connection_failure(RuntimeError('LAN read transfer failed: server not responding'))
    assert not connection_failure(ValueError('Alignment lost or duplicated primary reads'))


def test_paused_worker_does_not_claim_more_batches(monkeypatch):
    from scripts import julian_huref_align as align
    calls=[]
    class Client:
        def __init__(self,host):pass
        def call(self,op,*args):
            calls.append(op)
            return {'accepting_jobs':False}
        def close(self):pass
    monkeypatch.setattr(align,'AgentClient',Client)
    with pytest.raises(ConnectionError,match='Owner paused'):
        align.capacity({'host':'worker.local'})
    assert calls==['worker.policy']
