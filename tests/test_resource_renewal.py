"""Compatibility checks for Docker-backed workers, without renewable ownership."""
import json
import subprocess
import sys
import threading
from unittest.mock import patch
import pytest
from scripts import launcher_agent as module
from scripts.launcher_agent import Agent,ResourceLease,launcher_lock,docker_workers


def agent(tmp_path):
    a=Agent.__new__(Agent);a.root=tmp_path;a.config=tmp_path/'config.json';a.lock=threading.RLock()
    a.busy=lambda:None;a.pending_resources={}
    return a


def request():
    return {'id':'a'*32,'group':'genomics-test','cpus':1,'memory_mb':128,'container':'genomics-'+'a'*32}


def test_running_worker_is_rediscovered_without_lease_records(tmp_path,monkeypatch):
    live=[];monkeypatch.setattr(module,'docker_workers',lambda:live)
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    a=agent(tmp_path);args=request()
    assert a.dispatch('resource.acquire',args)['acquired']
    assert a.pending_resources
    live.append({**args,'id':'container-id','job':args['id'],'active':True,'created':0,'pids':32})
    assert a.dispatch('resource.renew',{'id':args['id']})['managed_by']=='docker'
    assert not a.pending_resources
    b=agent(tmp_path)
    assert b.dispatch('resource.renew',{'id':args['id']})['renewed']
    assert not list((tmp_path/'.resources').glob('*.json'))
    # Releasing a compatibility request cannot hide a running Docker worker.
    b.dispatch('resource.release',{'id':args['id']})
    assert b.reconcile_resources()==live
    live.clear()
    with pytest.raises(ValueError,match='not active'):b.dispatch('resource.renew',{'id':args['id']})


def test_drain_blocks_new_launches_without_interrupting_worker_checks(tmp_path,monkeypatch):
    args=request();live=[{**args,'id':'container-id','job':args['id']}]
    monkeypatch.setattr(module,'docker_workers',lambda:live)
    a=agent(tmp_path)
    with patch('scripts.launcher_agent.Path.exists',return_value=True):
        assert a.dispatch('resource.renew',{'id':args['id']})['renewed']
        with pytest.raises(RuntimeError,match='applying an explicit update'):
            a.dispatch('resource.acquire',{**args,'id':'c'*32,'container':'genomics-'+'c'*32})


def test_singleton_lock_fences_other_process_and_releases_on_exit(tmp_path):
    path=tmp_path/'launcher.lock';lock=launcher_lock(path)
    code='from scripts.launcher_agent import launcher_lock; import sys; lock=launcher_lock(sys.argv[1])'
    try:
        r=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True)
        assert r.returncode!=0 and 'already running' in r.stderr
    finally:lock.close()
    assert subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True).returncode==0


def test_docker_capacity_reads_limits_and_fails_closed_on_daemon_error(monkeypatch):
    calls=[]
    row={'Id':'f'*64,'Name':'/genomics-'+'a'*32,'State':{'Running':True},
         'Config':{'Labels':{'org.lifetech.job':'a'*32,'org.lifetech.group':'test'}},
         'HostConfig':{'NanoCpus':2_000_000_000,'Memory':0,'MemoryReservation':256*1024**2,'PidsLimit':32}}
    def run(cmd,**kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd,0,('f'*64+'\n') if cmd[1]=='ps' else json.dumps([row]),'')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert docker_workers()[0]['cpus']==2
    assert docker_workers()[0]['memory_mb']==256
    monkeypatch.setattr(module.subprocess,'run',lambda cmd,**kw:subprocess.CompletedProcess(cmd,1,'','daemon unavailable'))
    with pytest.raises(RuntimeError,match='Docker unavailable'):docker_workers()


def test_pending_admission_counts_docker_without_double_counting(tmp_path,monkeypatch):
    (tmp_path/'docker-workers').touch();limits={'cpus':2,'memory_mb':512,'available_mb':512}
    live=[];monkeypatch.setattr(module,'docker_workers',lambda:live)
    a=ResourceLease('test',1,128,'genomics-'+'a'*32,directory=tmp_path,limits=limits)
    b=ResourceLease('other',2,256,'genomics-'+'b'*32,directory=tmp_path,limits=limits)
    try:
        assert a.try_acquire()
        live.append({**a.row,'id':'docker-id'})
        assert not b.try_acquire()
        assert sum(r['memory_mb'] for r in b.rows() if r['active'])==128
        a.close()
        assert not b.try_acquire() # Docker worker still consumes a CPU.
        live.clear();assert b.try_acquire()
    finally:a.close();b.close()


def test_generic_worker_names_are_bound_to_the_admitted_job(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'docker_workers',lambda:[])
    monkeypatch.setattr(module,'worker_policy',lambda *_:{'accepting_jobs':True})
    monkeypatch.setattr(ResourceLease,'capacity',lambda *_:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    monkeypatch.setenv('SWARM_RESOURCE_DIR',str(tmp_path/'resources'))
    a=agent(tmp_path);args={**request(),'container':'swarm-job-'+'a'*32,'group':'image-processing'}
    assert a.dispatch('resource.acquire',args)['acquired']
    a.dispatch('resource.release',{'id':args['id']})
    with pytest.raises(ValueError,match='job-bound'):
        a.dispatch('resource.acquire',{**args,'container':'swarm-job-'+'b'*32})


def test_docker_inspection_timeout_keeps_reservation_queued(tmp_path,monkeypatch):
    import subprocess
    import scripts.launcher_agent as module
    monkeypatch.setattr(module.ResourceLease,'capacity',lambda self:{'cpus':4,'memory_mb':4096,'available_mb':4096})
    (tmp_path/'docker-workers').touch()
    lease=module.ResourceLease('queued-test',1,128,'fixture-worker',directory=tmp_path)
    try:
        def unavailable():raise subprocess.TimeoutExpired(['docker','inspect','worker'],15)
        monkeypatch.setattr(module,'docker_workers',unavailable)
        assert lease.try_acquire() is False
        assert not lease.row['active'] and lease.path.exists()
        monkeypatch.setattr(module,'docker_workers',lambda:[])
        assert lease.try_acquire() is True
    finally:lease.close()


def test_rpc_authentication_does_not_take_job_lock_and_still_rejects_replay(tmp_path,monkeypatch):
    import asyncio,time,secrets
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from unittest.mock import Mock
    class JobLock:
        def __enter__(self):raise AssertionError('RPC authentication blocked on job lock')
        def __exit__(self,*args):pass
    a=agent(tmp_path);a.lock=JobLock();a.nonce_lock=threading.Lock();a.nonces={}
    key=secrets.token_bytes(32)
    a.config.write_text(json.dumps({'name':'worker','master':'worker','peers':{'controller':'10.0.0.1'},'key':key.hex()}))
    a.telemetry=Mock();a.telemetry.accept.return_value={'accepted':True};a.telemetry_policy=lambda:{}
    monkeypatch.setattr(module,'verify_controller',lambda *_:None)
    value={'to':'worker','from':'controller','time':time.time(),'nonce':'a'*32,'op':'metrics.publish','args':{}}
    cipher=AESGCM(key);nonce=secrets.token_bytes(12)
    raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),module.VERSION.encode())
    reply=asyncio.run(a.process(raw));decoded=json.loads(cipher.decrypt(reply[:12],reply[12:],module.VERSION.encode()))
    assert decoded['ok'] and decoded['result']['accepted']
    with pytest.raises(ValueError,match='authentication rejected'):asyncio.run(a.process(raw))
