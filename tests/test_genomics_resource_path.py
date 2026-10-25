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
