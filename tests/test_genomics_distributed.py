import gzip
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from scripts.genomics_compute import compute
from scripts.genomics_distributed import batches,eligible,execute_reserved,merge,Run,memory_allocation,execute

HEADER='##fileformat=VCFv4.2\n##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: Allele|Consequence">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSYNTHETIC\n'
ROWS=['1\t1\t.\tA\tG\t30\tPASS\tCSQ=G|missense_variant,G|missense_variant&splice_region_variant\tGT:DP:GQ\t0/1:25:40\n',
      '2\t2\t.\tC\tA\t30\tq10\tCSQ=A|intron_variant\tGT:DP:GQ\t1/1:8:9\n']
def payload(rows=ROWS):return gzip.compress((HEADER+''.join(rows)).encode())

class DistributedGenomicsTests(unittest.TestCase):
    def test_qc_has_expected_allele_genotype_and_depth_statistics(self):
        result=compute(payload(),'qc')
        self.assertEqual(result['records'],2);self.assertEqual(result['transitions'],1);self.assertEqual(result['transversions'],1)
        self.assertEqual(result['genotypes'],{'heterozygous':1,'hom_alternate':1})
        self.assertEqual(result['depth_bins'],{'20-29':1,'1-9':1});self.assertEqual(result['depth_sum'],33)

    def test_annotation_deduplicates_each_consequence_within_record(self):
        result=compute(payload(),'annotation')
        self.assertEqual(result['records_with_csq'],2)
        self.assertEqual(result['consequences'],{'missense_variant':1,'splice_region_variant':1,'intron_variant':1})
        self.assertNotIn('SYNTHETIC',json.dumps(result));self.assertNotIn('0/1',json.dumps(result))

    def test_batches_preserve_every_record_and_respect_contig_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'x.vcf.gz';source.write_bytes(payload())
            jobs=list(batches(source,'qc',10))
            self.assertEqual(len(jobs),2);self.assertEqual(sum(j['records']for j in jobs),2)
            actual=merge([compute(j['payload'],'qc')for j in jobs],'qc')
            expected=compute(payload(),'qc')
            for key in ['records','depth_sum','genotypes','filters','transitions']:self.assertEqual(actual[key],expected[key])

    def test_real_worker_process_reads_stdin_and_leaves_no_files(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'x.vcf.gz';source.write_bytes(payload())
            job=next(batches(source,'annotation',1))
            import scripts.genomics_distributed as runner
            response=subprocess.run(['python3','-c',runner.WORKER,job['kind']],input=job['payload'],capture_output=True,check=True)
            result=json.loads(response.stdout)
            self.assertEqual(result['records'],1);self.assertEqual(result['sha256'],job['sha256'])
            self.assertEqual(list(Path(d).iterdir()),[source])

    def test_untrusted_or_declining_agent_is_not_eligible(self):
        status={'ready':True,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'active_jobs':[]}
        self.assertTrue(eligible(status));self.assertTrue(eligible({**status,'active_jobs':['run']}))
        self.assertFalse(eligible({**status,'worker_policy':{'accepting_jobs':False}}))
        self.assertFalse(eligible({**status,'protocol':'untrusted'}))

    def test_remote_transport_uses_fixed_program_and_options_before_destination(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'x.vcf.gz';source.write_bytes(payload());job=next(batches(source,'qc',1))
            reply={**compute(job['payload'],'qc'),'sha256':job['sha256']}
            with patch('scripts.genomics_distributed.subprocess.Popen',return_value=Mock(returncode=0,poll=Mock(return_value=0),communicate=Mock(return_value=(json.dumps(reply).encode(),b'')))) as run:
                execute_reserved({'host':'node.local','local':False,'image':'sha256:test','ssh':['ssh','-o','StrictHostKeyChecking=yes','user@node.local']},job,'genomics-fixture',1536)
            command=run.call_args.args[0]
            self.assertLess(command.index('ServerAliveInterval=15'),command.index('user@node.local'))
            self.assertNotIn('shell',run.call_args.kwargs)
            self.assertIn('--memory 1536m --memory-swap 1536m',command[-1])
            self.assertEqual(run.return_value.communicate.call_args.args[0],job['payload'])

    def test_result_substitution_rejected(self):
        from types import SimpleNamespace
        with patch('scripts.genomics_distributed.subprocess.Popen',return_value=Mock(returncode=0,poll=Mock(return_value=0),communicate=Mock(return_value=(b'{"sha256":"wrong"}',b'')))):
            with self.assertRaisesRegex(RuntimeError,'does not match'):execute_reserved({'host':'local','local':True,'image':'sha256:test'},{'kind':'qc','records':1,'payload':payload(),'sha256':'expected'},'genomics-fixture',1536)

    def test_memory_budget_tracks_live_capacity_remaining_concurrency_and_queue(self):
        self.assertEqual(memory_allocation({'available_mb':24000,'group_active_jobs':0,'waiting_jobs':0},4),6000)
        self.assertEqual(memory_allocation({'available_mb':18000,'group_active_jobs':1,'waiting_jobs':0},4),6000)
        self.assertEqual(memory_allocation({'available_mb':18000,'group_active_jobs':1,'waiting_jobs':3},4),3000)
        self.assertEqual(memory_allocation({'available_mb':751,'group_active_jobs':0,'waiting_jobs':0},1),751)
        from scripts.genomics_distributed import CapacityWait
        with self.assertRaises(CapacityWait):memory_allocation({'available_mb':0},4)

    def test_worker_enforcement_matches_the_actual_capacity_reservation(self):
        from types import SimpleNamespace
        import sys
        client=Mock()
        client.call.side_effect=[{'available_mb':20000,'group_active_jobs':0,'waiting_jobs':1},{'acquired':True},{'released':True}]
        host={'_concurrency':3}
        with patch.dict(sys.modules,{'agent_client':SimpleNamespace(AgentClient=Mock(return_value=client))}),patch('scripts.genomics_distributed.execute_reserved',return_value={'passed':True}) as reserved:
            self.assertEqual(execute(host,{}),{'passed':True})
        self.assertEqual(client.call.call_args_list[1].args[1]['memory_mb'],5000)
        self.assertEqual(reserved.call_args.args[-1],5000)
        self.assertEqual(client.call.call_args_list[-1].args[0],'resource.release')

    def test_reference_blocks_cannot_be_counted_as_alternate_variants(self):
        with self.assertRaisesRegex(ValueError,'prepared'):compute(payload([ROWS[0].replace('\tG\t','\t<NON_REF>\t')]),'qc')

    def test_bad_input_never_echoes_private_content_to_stderr(self):
        import scripts.genomics_distributed as runner
        result=subprocess.run(['python3','-c',runner.WORKER,'qc'],input=gzip.compress(b'PRIVATE-SAMPLE-DATA\n'),capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertNotIn(b'PRIVATE',result.stderr);self.assertEqual(result.stdout,b'')

    def test_temporary_unavailability_requeues_instead_of_failing_batch(self):
        import scripts.genomics_distributed as runner
        def response(host,batch):return {**compute(batch['payload'],batch['kind']),'sha256':batch['sha256'],'host':host['host'],'elapsed_seconds':.1,'input_bytes':len(batch['payload'])}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.vcf.gz';source.write_bytes(payload([ROWS[0]]))
            run=Run([{'host':'fixture','local':True,'eligible':True}],root,slots=1)
            with patch.object(run,'capacity',side_effect=[False,True]),patch.object(runner,'execute',side_effect=response),patch.object(runner.time,'sleep'):
                run.run({'qc':source},10)
            self.assertEqual(run.state['status'],'passed')
            self.assertEqual(len(run.results),1)

    def test_resume_verifies_and_reuses_completed_payloads(self):
        import scripts.genomics_distributed as runner
        import hashlib
        def response(host,batch):return {**compute(batch['payload'],batch['kind']),'sha256':batch['sha256'],'host':host['host'],'elapsed_seconds':.1,'input_bytes':len(batch['payload'])}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.vcf.gz';source.write_bytes(payload())
            jobs=list(batches(source,'qc',1));cached=response({'host':'old-worker'},jobs[0]);(root/'qc-1.json').write_text(json.dumps(cached))
            previous={'status':'failed','worker_source_sha256':hashlib.sha256(runner.WORKER.encode()).hexdigest(),'sources':{}}
            (root/'state.json').write_text(json.dumps(previous))
            run=Run([{'host':'fixture','local':True,'eligible':True}],root,slots=1,resume=True)
            with patch.object(run,'capacity',return_value=True),patch.object(runner,'execute',side_effect=response)as execute:
                run.run({'qc':source},1)
            self.assertEqual(execute.call_count,1)
            self.assertTrue(run.state['jobs']['qc-1']['reused'])
            self.assertEqual(json.loads((root/'qc-summary.json').read_text())['records'],2)
            wrong={**jobs[0],'sha256':'different'}
            with self.assertRaisesRegex(ValueError,'differs'):run.cached(wrong)

    def test_unknown_cpu_or_memory_is_not_treated_as_spare_capacity(self):
        from scripts.genomics_distributed import has_capacity
        measured={'percent':20,'ram_total_bytes':4*1024**3,'ram_used_bytes':1024**3}
        self.assertTrue(has_capacity(measured))
        for missing in ['percent','ram_total_bytes','ram_used_bytes']:
            self.assertFalse(has_capacity({**measured,missing:None}))
        self.assertTrue(has_capacity({**measured,'percent':90}))
