import json
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
            report=json.loads((Path(folder)/'status.json').read_text())
            self.assertEqual(report['exit_code'],1)
            attempt=Path(report['attempt_report'])
            self.assertTrue(attempt.exists())
            self.assertTrue(attempt.with_suffix('.stderr').exists())
            self.assertEqual(json.loads(attempt.read_text())['container'],report['container'])
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

class AdmissionTests(unittest.TestCase):
    def test_merge_can_queue_for_capacity_without_starting_unleased_work(self):
        from scripts.wgs_recall_swarm import leased_run
        from unittest.mock import MagicMock
        operations=[];admitted=[];requests=[]
        def call(op,args=None,**kwargs):
            operations.append(op)
            if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
            if op=='resource.capacity':return {'available_mb':0,'available_cpus':0}
            if op=='resource.acquire':requests.append(args);return {'acquired':len(requests)>1}
            return {}
        transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
        with tempfile.TemporaryDirectory() as folder,patch('scripts.wgs_recall_swarm.AgentClient') as client,patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True),patch('scripts.wgs_recall_swarm.time.sleep'),patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport),patch('scripts.wgs_recall_swarm.cleanup_container',return_value=True):
            client.return_value.call.side_effect=call
            leased_run({'host':'peer','machine':'id','local':True},'sha256:pinned','true',[],folder,2,1024,Path(folder)/'status.json',wait_for_capacity=True,on_admitted=lambda:admitted.append(True))
        self.assertEqual(len(requests),2);self.assertEqual(requests[0],requests[1]);self.assertEqual(admitted,[True]);self.assertIn('resource.release',operations)


def test_failed_attempt_keeps_oom_evidence_across_retry(tmp_path):
    from unittest.mock import MagicMock
    import subprocess
    import pytest
    from scripts.wgs_recall_swarm import leased_run
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=137;transport.poll.return_value=137
    inspected=subprocess.CompletedProcess([],0,json.dumps({'Status':'exited','ExitCode':137,'OOMKilled':True,'Error':''}),'')
    with patch('scripts.wgs_recall_swarm.AgentClient') as client, patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True), patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport) as launch, patch('scripts.wgs_recall_swarm.subprocess.run',return_value=inspected), patch('scripts.wgs_recall_swarm.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        for _ in range(2):
            with pytest.raises(RuntimeError,match='OOM=True'):
                leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','false',[],str(tmp_path),1,1024,tmp_path/'status.json')
        assert '--rm' not in launch.call_args.args[0]
    attempts=list((tmp_path/'attempts').glob('*.json'))
    assert len(attempts)==2
    for p in attempts:
        assert json.loads(p.read_text())['container_state']['OOMKilled'] is True
        assert p.with_suffix('.stderr').exists()


def test_owner_stop_requeues_stage_without_alignment_failure(tmp_path):
    from unittest.mock import MagicMock
    import pytest
    from scripts.wgs_recall_swarm import leased_run
    from scripts.genomics_distributed import CapacityWait
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        if op=='worker.policy':return {'accepting_jobs':False,'mode':'paused'}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=143;transport.poll.return_value=143
    with patch('scripts.wgs_recall_swarm.AgentClient') as client,patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True),patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport),patch('scripts.wgs_recall_swarm.subprocess.run',return_value=__import__('subprocess').CompletedProcess([],0,'{}','')),patch('scripts.wgs_recall_swarm.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        with pytest.raises(CapacityWait,match='Owner paused'):
            leased_run({'host':'worker.local','local':True,'machine':'id'},'sha256:pinned','align',[],str(tmp_path),1,1024,tmp_path/'status.json')
        assert any(c.args[0]=='resource.release' for c in client.return_value.call.call_args_list)
        assert json.loads((tmp_path/'status.json').read_text())['phase']=='interrupted'


def test_ram_scratch_is_bounded_inside_the_reserved_non_swapping_container(tmp_path):
    from unittest.mock import MagicMock,patch
    from scripts.wgs_recall_swarm import leased_run
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':16384,'available_cpus':8}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
    with patch('scripts.wgs_recall_swarm.AgentClient') as client,patch('scripts.wgs_recall_swarm.distributed.eligible',return_value=True),patch('scripts.wgs_recall_swarm.subprocess.Popen',return_value=transport) as launched,patch('scripts.wgs_recall_swarm.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),2,8192,tmp_path/'status.json',tmpfs={'/ram':2048})
        argv=launched.call_args.args[0]
        assert argv[argv.index('--memory')+1]=='8192m'
        assert argv[argv.index('--memory-swap')+1]=='8192m'
        assert argv[argv.index('--tmpfs')+1]=='/ram:rw,nosuid,nodev,noexec,size=2048m,mode=1777'
