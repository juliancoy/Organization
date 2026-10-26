"""The launcher must reserve capacity inside its writable job-data mount."""
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('genomics_agent_resource_path',Path(__file__).resolve().parents[1]/'scripts/launcher_agent.py')
agent_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent_module)

class GenomicsResourcePathTests(unittest.TestCase):
    def test_annotation_memory_is_reserved_atomically_and_remains_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent=agent_module.Agent.__new__(agent_module.Agent)
            agent.root=Path(temporary);agent.lock=threading.RLock();agent.config=agent.root/'config.json';agent.busy=lambda:None
            first={'id':'a'*32,'group':'genomics-vep','cpus':1,'memory_mb':2048,'container':'genomics-'+'b'*32}
            second={'id':'c'*32,'group':'genomics-vep','cpus':1,'memory_mb':1024,'container':'genomics-'+'d'*32}
            with patch.object(agent_module.ResourceLease,'capacity',return_value={'cpus':8,'memory_mb':2560,'available_mb':2560}),patch.object(agent_module,'worker_policy',return_value={'accepting_jobs':True}):
                try:
                    self.assertTrue(agent.dispatch('resource.acquire',first)['acquired'])
                    self.assertFalse(agent.dispatch('resource.acquire',second)['acquired'])
                    agent.dispatch('resource.release',{'id':first['id']})
                    self.assertTrue(agent.dispatch('resource.acquire',second)['acquired'])
                    for memory in (True,1024.0,0,-1):
                        with self.assertRaisesRegex(ValueError,'bounded'):
                            agent.dispatch('resource.acquire',{**first,'memory_mb':memory})
                    with self.assertRaisesRegex(ValueError,'capacity'):
                        agent.dispatch('resource.acquire',{**first,'memory_mb':4096})
                    self.assertNotIn(first['id'],agent.resource_leases)
                    self.assertTrue(agent.dispatch('resource.acquire',{**first,'memory_mb':768})['acquired'])
                finally:
                    for args in (first,second):agent.dispatch('resource.release',{'id':args['id']})

    def test_capacity_snapshot_counts_actual_reservations_and_waiters(self):
        with tempfile.TemporaryDirectory() as temporary:
            limits={'cpus':4,'memory_mb':12000,'available_mb':9000}
            first=agent_module.ResourceLease('genomics-fixture',1,5000,directory=temporary,limits=limits)
            queued=agent_module.ResourceLease('other',1,8000,directory=temporary,limits=limits)
            try:
                self.assertTrue(first.try_acquire())
                self.assertFalse(queued.try_acquire())
                with patch.object(agent_module.ResourceLease,'capacity',return_value=limits):
                    snapshot=agent_module.ResourceLease.pool_capacity(temporary,'genomics-fixture')
                self.assertEqual(snapshot['available_mb'],4000)
                self.assertEqual(snapshot['reserved_mb'],5000)
                self.assertEqual(snapshot['group_active_jobs'],1)
                self.assertEqual(snapshot['waiting_jobs'],1)
                self.assertEqual(len(list(Path(temporary).glob('*.json'))),2)
            finally:first.close();queued.close()

    def test_background_usage_and_unused_reservations_cannot_be_pledged_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            # 20 GiB of nonpool/background pressure leaves only 12 GiB available.
            limits={'cpus':8,'memory_mb':32768,'available_mb':12288}
            first=agent_module.ResourceLease('genomics-fixture',1,8192,directory=temporary,limits=limits)
            second=agent_module.ResourceLease('other',1,5120,directory=temporary,limits=limits)
            fitting=agent_module.ResourceLease('other',1,4096,directory=temporary,limits=limits)
            try:
                self.assertTrue(first.try_acquire())
                # The first reservation may not have grown its RSS yet. Its full
                # headroom still belongs to that worker, not the next one.
                self.assertFalse(second.try_acquire())
                with patch.object(agent_module.ResourceLease,'capacity',return_value=limits):
                    snapshot=agent_module.ResourceLease.pool_capacity(temporary,'genomics-fixture')
                self.assertEqual(snapshot['available_mb'],4096)
                self.assertTrue(fitting.try_acquire())
                with patch.object(agent_module.ResourceLease,'capacity',return_value=limits):
                    self.assertEqual(agent_module.ResourceLease.pool_capacity(temporary)['available_mb'],0)
            finally:first.close();second.close();fitting.close()

    def test_launcher_private_memory_cap_does_not_limit_sibling_workers(self):
        from types import SimpleNamespace
        container='docker-'+'a'*64+'.scope'
        texts={
            '/proc/meminfo':'MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n',
            '/proc/self/cgroup':'0::/user.slice/'+container+'\n',
            '/sys/fs/cgroup/user.slice/'+container+'/memory.max':str(768*1024**2),
            '/sys/fs/cgroup/user.slice/'+container+'/memory.current':str(500*1024**2),
            '/sys/fs/cgroup/user.slice/memory.max':str(4096*1024**2),
            '/sys/fs/cgroup/user.slice/memory.current':str(1024*1024**2),
            '/sys/fs/cgroup/memory.max':'max',
        }
        def read(path,*args,**kwargs):
            if str(path) in texts:return texts[str(path)]
            raise FileNotFoundError(str(path))
        with patch.object(agent_module.Path,'read_text',read):
            limits=agent_module.ResourceLease.capacity(SimpleNamespace(limits=None))
        self.assertEqual(limits['memory_mb'],4096)
        self.assertEqual(limits['available_mb'],3072)

    def test_resource_pool_does_not_require_a_writable_container_home(self):
        with tempfile.TemporaryDirectory()as temporary:
            agent=agent_module.Agent.__new__(agent_module.Agent)
            agent.root=Path(temporary);agent.lock=threading.RLock();agent.config=agent.root/'config.json';agent.busy=lambda:None
            ident='a'*32;args={'id':ident,'group':'genomics-fixture','cpus':1,'memory_mb':512,'container':'genomics-'+'b'*32}
            environment={key:value for key,value in os.environ.items()if key!='SWARM_RESOURCE_DIR'}
            with patch.dict(os.environ,environment,clear=True),patch.object(agent_module.Path,'home',return_value=Path('/proc/unwritable-genomics-fixture')),patch.object(agent_module.ResourceLease,'capacity',return_value={'cpus':2,'memory_mb':2048,'available_mb':2048}),patch.object(agent_module,'worker_policy',return_value={'accepting_jobs':True}):
                try:
                    self.assertTrue(agent.dispatch('resource.acquire',args)['acquired'])
                    files=list((agent.root/'.resources').glob('*.json'));self.assertEqual(len(files),1)
                    self.assertEqual(files[0].stat().st_mode&0o777,0o600)
                finally:agent.dispatch('resource.release',{'id':ident})
            self.assertEqual(list((agent.root/'.resources').glob('*.json')),[])
