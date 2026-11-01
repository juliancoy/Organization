"""Real Docker evidence for worker discovery and host/container singleton fencing."""
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import uuid
from scripts.launcher_agent import Agent,launcher_lock

IMAGE='python:3.12-bookworm'


def test_worker_survives_launcher_close_and_is_rediscovered(tmp_path,monkeypatch):
    ident=uuid.uuid4().hex;name='genomics-'+ident
    monkeypatch.setenv('SWARM_RESOURCE_DIR',str(tmp_path/'resources'))
    (tmp_path/'resources').mkdir();(tmp_path/'resources/docker-workers').touch()
    def agent():
        a=Agent.__new__(Agent);a.root=tmp_path;a.config=tmp_path/'config.json';a.lock=threading.RLock()
        a.pending_resources={};a.busy=lambda:None;a.telemetry=SimpleNamespace(stopped=threading.Event())
        a.storage_stop=threading.Event();a.browser_timers={};a.browsers=set();a.jobs={}
        return a
    cid=subprocess.check_output(['docker','run','-d','--rm','--pull=never','--name',name,
        '--label','org.lifetech.worker=true','--label','org.lifetech.job='+ident,
        '--label','org.lifetech.group=restart-test','--cpus','1','--memory','64m','--pids-limit','32',
        '--network','none',IMAGE,'python','-c','import time; time.sleep(120)'],text=True).strip()
    try:
        before=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]['State']['StartedAt']
        a=agent();assert any(w['job']==ident for w in a.reconcile_resources())
        a.close()
        b=agent();assert b.dispatch('resource.renew',{'id':ident})['managed_by']=='docker'
        state=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]['State']
        assert state['Running'] and state['StartedAt']==before
        assert not list((tmp_path/'resources').glob('*.json'))
    finally:subprocess.run(['docker','rm','-f',cid],check=True,capture_output=True)


def test_docker_and_host_share_launcher_lock(tmp_path):
    path=tmp_path/'agent.lock';held=launcher_lock(path)
    code="import fcntl; f=open('/config/agent.lock','a+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    cmd=['docker','run','--rm','--pull=never','--network','none','--mount','type=bind,src='+str(tmp_path)+',dst=/config',IMAGE,'python','-c',code]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        assert r.returncode!=0 and 'BlockingIOError' in r.stderr
    finally:held.close()
    assert subprocess.run(cmd,capture_output=True,timeout=30).returncode==0
