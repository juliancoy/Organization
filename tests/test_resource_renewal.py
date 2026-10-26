import threading
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts.launcher_agent import Agent,ResourceLease


def test_long_work_can_renew_without_stale_timer_killing_it(tmp_path):
    agent=Agent.__new__(Agent);agent.root=tmp_path;agent.config=tmp_path/'config.json';agent.lock=threading.RLock();agent.busy=lambda:None
    args={'id':'a'*32,'group':'genomics-long','cpus':4,'memory_mb':4096,'container':'genomics-'+'b'*32}
    with patch.object(ResourceLease,'capacity',return_value={'cpus':8,'memory_mb':12000,'available_mb':12000}),patch('scripts.launcher_agent.worker_policy',return_value={'accepting_jobs':True}):
        try:
            assert agent.dispatch('resource.acquire',args)['acquired']
            original=agent.resource_leases[args['id']]['token']
            assert agent.dispatch('resource.renew',{'id':args['id']})['renewed']
            renewed=agent.resource_leases[args['id']]['token'];assert renewed!=original
            with patch('scripts.launcher_agent.subprocess.run') as remove:
                agent.expire_resource(args['id'],original);remove.assert_not_called()
            assert args['id'] in agent.resource_leases
            snap=ResourceLease.pool_capacity(tmp_path/'.resources')
            assert snap['available_cpus']==4
            with pytest.raises(ValueError,match='capacity'):
                agent.dispatch('resource.acquire',{**args,'id':'c'*32,'cpus':9})
        finally:agent.dispatch('resource.release',{'id':args['id']})
    with pytest.raises(ValueError,match='not active'):
        agent.dispatch('resource.renew',{'id':args['id']})


def test_cancelled_queue_timer_cannot_expire_new_active_generation(tmp_path):
    agent=Agent.__new__(Agent);agent.root=tmp_path;agent.config=tmp_path/'config.json';agent.lock=threading.RLock();agent.busy=lambda:None
    callbacks=[]
    class Timer:
        def __init__(self,seconds,callback):callbacks.append(callback)
        def start(self):pass
        def cancel(self):pass
    args={'id':'a'*32,'group':'genomics-long','cpus':2,'memory_mb':1000,'container':'genomics-'+'b'*32}
    with patch.object(ResourceLease,'capacity',return_value={'cpus':8,'memory_mb':12000,'available_mb':12000}),patch('scripts.launcher_agent.worker_policy',return_value={'accepting_jobs':True}),patch('scripts.launcher_agent.threading.Timer',Timer):
        try:
            assert agent.dispatch('resource.acquire',args)['acquired']
            with patch('scripts.launcher_agent.subprocess.run') as remove:
                callbacks[0]();remove.assert_not_called()
            assert args['id'] in agent.resource_leases
        finally:agent.dispatch('resource.release',{'id':args['id']})
