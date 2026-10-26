import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.wgs_recall_swarm import copy_receipt

class TransferTests(unittest.TestCase):
    def test_modified_source_never_becomes_verified_final(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'corrupted')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.wgs_recall_swarm.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                with self.assertRaisesRegex(RuntimeError,'SHA256 mismatch'):copy_receipt(first,second,receipt)
            self.assertFalse((Path(second['cache'])/'incoming'/receipt['sha256']/source.name).exists())
    def test_verified_copy_matches_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'expected!')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.wgs_recall_swarm.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                result=copy_receipt(first,second,receipt)
            self.assertEqual(Path(result['path']).read_bytes(),b'expected!')
            self.assertEqual(result['original_host'],'source')

class CleanupTests(unittest.TestCase):
    def test_dead_ssh_still_removes_container_before_releasing_lease(self):
        from scripts.wgs_recall_swarm import leased_run
        from unittest.mock import MagicMock
        events=[]
        with tempfile.TemporaryDirectory() as folder:
            host={'host':'local','local':True,'machine':'id'}
            def call(operation,*args,**kwargs):
                events.append(operation)
                if operation=='status':return {'machine':'id','capabilities':['renewable-resources']}
                if operation=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
                if operation=='resource.acquire':return {'acquired':True}
                return {}
            transport=MagicMock();transport.wait.return_value=1;transport.poll.return_value=1
            with patch('scripts.wgs_recall_swarm.AgentClient') as client,patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True),patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport),patch('scripts.wgs_recall_swarm.cleanup_container',side_effect=lambda *a:events.append('container_removed') or True):
                client.return_value.call.side_effect=call
                with self.assertRaisesRegex(RuntimeError,'Worker stage failed'):leased_run(host,'sha256:pinned','false',[],folder,1,1024,Path(folder)/'status.json')
            self.assertLess(events.index('container_removed'),events.index('resource.release'))
    def test_failed_cleanup_keeps_lease_reserved(self):
        from scripts.wgs_recall_swarm import leased_run
        from unittest.mock import MagicMock
        operations=[]
        with tempfile.TemporaryDirectory() as folder:
            def call(operation,*args,**kwargs):
                operations.append(operation)
                return {'machine':'id','capabilities':['renewable-resources']} if operation=='status' else {'available_mb':4096,'available_cpus':4} if operation=='resource.capacity' else {'acquired':True}
            transport=MagicMock();transport.wait.return_value=1;transport.poll.return_value=1
            with patch('scripts.wgs_recall_swarm.AgentClient') as client,patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True),patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport),patch('scripts.wgs_recall_swarm.cleanup_container',return_value=False):
                client.return_value.call.side_effect=call
                with self.assertRaisesRegex(RuntimeError,'lease retained'):leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','false',[],folder,1,1024,Path(folder)/'status.json')
            self.assertNotIn('resource.release',operations)
