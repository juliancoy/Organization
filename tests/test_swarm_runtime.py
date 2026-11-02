import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.swarm_runtime import copy_verified_file as copy_receipt

class TransferTests(unittest.TestCase):
    def test_modified_source_never_becomes_verified_final(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'corrupted')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                with self.assertRaisesRegex(RuntimeError,'SHA256 mismatch'):copy_receipt(first,second,receipt)
            self.assertFalse((Path(second['cache'])/'incoming'/receipt['sha256']/source.name).exists())
    def test_verified_copy_matches_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'public.fastq.gz';source.write_bytes(b'expected!')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                result=copy_receipt(first,second,receipt)
            self.assertEqual(Path(result['path']).read_bytes(),b'expected!')
            self.assertEqual(result['original_host'],'source')

    def test_cached_copy_never_opens_source_stream_and_corruption_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.bin';source.write_bytes(b'expected!')
            first={'host':'source','local':True,'machine':'id'}
            second={'host':'target','local':True,'cache':str(Path(folder)/'cache')}
            receipt={'host':'source','path':str(source),'sha256':hashlib.sha256(b'expected!').hexdigest(),'bytes':9}
            with patch('scripts.swarm_runtime.AgentClient') as client:
                client.return_value.call.return_value={'machine':'id'}
                result=copy_receipt(first,second,receipt)
                import subprocess
                real_popen=subprocess.Popen
                def probe_only(args, **kwargs):
                    if args[-1]!='probe':raise AssertionError('Cache hit opened source stream')
                    return real_popen(args, **kwargs)
                with patch('scripts.swarm_runtime.subprocess.Popen',side_effect=probe_only):
                    self.assertEqual(copy_receipt(first,second,receipt),result)
                Path(result['path']).write_bytes(b'corrupted')
                copy_receipt(first,second,receipt)
                self.assertEqual(Path(result['path']).read_bytes(),b'expected!')

class CleanupTests(unittest.TestCase):
    def test_dead_ssh_still_removes_container_before_releasing_lease(self):
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
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
            with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',side_effect=lambda *a:events.append('container_removed') or True):
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
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
        from unittest.mock import MagicMock
        operations=[]
        with tempfile.TemporaryDirectory() as folder:
            def call(operation,*args,**kwargs):
                operations.append(operation)
                return {'machine':'id','capabilities':['renewable-resources']} if operation=='status' else {'available_mb':4096,'available_cpus':4} if operation=='resource.capacity' else {'acquired':True}
            transport=MagicMock();transport.wait.return_value=1;transport.poll.return_value=1
            with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',return_value=False):
                client.return_value.call.side_effect=call
                with self.assertRaisesRegex(RuntimeError,'lease retained'):leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','false',[],folder,1,1024,Path(folder)/'status.json')
            self.assertNotIn('resource.release',operations)

class AdmissionTests(unittest.TestCase):
    def test_merge_can_queue_for_capacity_without_starting_unleased_work(self):
        from functools import partial
        from scripts.swarm_runtime import run_job
        leased_run=partial(run_job,group='test')
        from unittest.mock import MagicMock
        operations=[];admitted=[];requests=[]
        def call(op,args=None,**kwargs):
            operations.append(op)
            if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
            if op=='resource.capacity':return {'available_mb':0,'available_cpus':0}
            if op=='resource.acquire':requests.append(args);return {'acquired':len(requests)>1}
            return {}
        transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
        with tempfile.TemporaryDirectory() as folder,patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.time.sleep'),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.cleanup_container',return_value=True):
            client.return_value.call.side_effect=call
            leased_run({'host':'peer','machine':'id','local':True},'sha256:pinned','true',[],folder,2,1024,Path(folder)/'status.json',wait_for_capacity=True,on_admitted=lambda:admitted.append(True))
        self.assertEqual(len(requests),2);self.assertEqual(requests[0],requests[1]);self.assertEqual(admitted,[True]);self.assertIn('resource.release',operations)


def test_failed_attempt_keeps_oom_evidence_across_retry(tmp_path):
    from unittest.mock import MagicMock
    import subprocess
    import pytest
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=137;transport.poll.return_value=137
    inspected=subprocess.CompletedProcess([],0,json.dumps({'Status':'exited','ExitCode':137,'OOMKilled':True,'Error':''}),'')
    with patch('scripts.swarm_runtime.AgentClient') as client, patch('scripts.swarm_runtime.eligible',return_value=True), patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport) as launch, patch('scripts.swarm_runtime.subprocess.run',return_value=inspected), patch('scripts.swarm_runtime.cleanup_container',return_value=True):
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
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    from scripts.genomics_distributed import CapacityWait
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        if op=='worker.policy':return {'accepting_jobs':False,'mode':'paused'}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=143;transport.poll.return_value=143
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport),patch('scripts.swarm_runtime.subprocess.run',return_value=__import__('subprocess').CompletedProcess([],0,'{}','')),patch('scripts.swarm_runtime.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        with pytest.raises(CapacityWait,match='Owner paused'):
            leased_run({'host':'worker.local','local':True,'machine':'id'},'sha256:pinned','align',[],str(tmp_path),1,1024,tmp_path/'status.json')
        assert any(c.args[0]=='resource.release' for c in client.return_value.call.call_args_list)
        assert json.loads((tmp_path/'status.json').read_text())['phase']=='interrupted'


def test_ram_scratch_is_bounded_inside_the_reserved_non_swapping_container(tmp_path):
    from unittest.mock import MagicMock,patch
    from functools import partial
    from scripts.swarm_runtime import run_job
    leased_run=partial(run_job,group='test')
    def call(op,*args,**kwargs):
        if op=='status':return {'machine':'id','capabilities':['renewable-resources','docker-workers']}
        if op=='resource.capacity':return {'available_mb':16384,'available_cpus':8}
        return {'acquired':True}
    transport=MagicMock();transport.wait.return_value=0;transport.poll.return_value=0
    with patch('scripts.swarm_runtime.AgentClient') as client,patch('scripts.swarm_runtime.eligible',return_value=True),patch('scripts.swarm_runtime.subprocess.Popen',return_value=transport) as launched,patch('scripts.swarm_runtime.cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        leased_run({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),2,8192,tmp_path/'status.json',tmpfs={'/ram':2048})
        argv=launched.call_args.args[0]
        assert argv[argv.index('--memory')+1]=='8192m'
        assert argv[argv.index('--memory-swap')+1]=='8192m'
        assert argv[argv.index('--tmpfs')+1]=='/ram:rw,nosuid,nodev,noexec,size=2048m,mode=1777'


def test_runtime_import_has_no_genome_application_dependencies():
    import subprocess,sys
    code="import sys;from scripts import swarm_runtime;assert not any(n.startswith(('scripts.genomics','scripts.wgs','scripts.julian','scripts.huref','scripts.genome_')) for n in sys.modules)"
    subprocess.run([sys.executable,'-c',code],check=True,capture_output=True)


def test_generic_jobs_use_their_own_group_and_no_genome_labels(tmp_path):
    from unittest.mock import MagicMock
    from scripts import swarm_runtime as runtime
    def call(op,args=None,**kwargs):
        if op=='status':return {'machine':'id','ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'capabilities':['renewable-resources','docker-workers','generic-workers']}
        if op=='resource.capacity':return {'available_mb':4096,'available_cpus':4}
        return {'acquired':True}
    process=MagicMock();process.wait.return_value=0
    with patch.object(runtime,'AgentClient') as client,patch.object(runtime.subprocess,'Popen',return_value=process) as launch,patch.object(runtime,'cleanup_container',return_value=True):
        client.return_value.call.side_effect=call
        runtime.run_job({'host':'local','local':True,'machine':'id'},'sha256:pinned','true',[],str(tmp_path),1,1024,tmp_path/'status.json',group='image-processing',labels={'org.example.pipeline':'thumbnail'})
    argv=launch.call_args.args[0]
    assert argv[argv.index('--name')+1].startswith('swarm-job-')
    assert 'org.lifetech.group=image-processing' in argv
    assert 'org.example.pipeline=thumbnail' in argv
    assert 'org.lifetech.genomics=true' not in argv
    acquisition=next(c for c in client.return_value.call.call_args_list if c.args[0]=='resource.acquire')
    assert acquisition.args[1]['group']=='image-processing'
    assert acquisition.args[1]['container'].startswith('swarm-job-')


def test_combined_ram_scratch_is_checked_before_contacting_a_worker(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    with patch.object(runtime,'AgentClient') as client:
        with pytest.raises(ValueError,match='Combined RAM scratch'):
            runtime.run_job({},'image','command',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test',tmpfs={'/first':600,'/second':600})
        client.assert_not_called()


def test_application_cannot_change_runtime_identity_labels(tmp_path):
    import pytest
    from scripts import swarm_runtime as runtime
    with patch.object(runtime,'AgentClient') as client:
        with pytest.raises(ValueError,match='identity labels'):
            runtime.run_job({},'image','command',[],str(tmp_path),1,1024,tmp_path/'status.json',group='test',labels={'org.lifetech.group':'someone-else'})
        client.assert_not_called()
