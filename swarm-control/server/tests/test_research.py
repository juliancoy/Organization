import json
from pathlib import Path
import tempfile
import unittest
from research import snapshot


class ResearchTests(unittest.TestCase):
    def test_followup_exports_only_named_operational_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'followup/run-example'
            folder.mkdir(parents=True)
            (folder / 'state.json').write_text(json.dumps({'status': 'running', 'source': '/PRIVATE',
                'jobs': {'bam_coverage': {'status': 'running', 'path': '/PRIVATE'},
                         'gvcf_span_audit': {'status': 'PRIVATE'}, 'PRIVATE': {'status': 'passed'}}}))
            result = snapshot(root)['followup']
            self.assertEqual(result['jobs']['bam_coverage']['status'], 'running')
            self.assertEqual(result['jobs']['gvcf_span_audit']['status'], 'unknown')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_only_operational_allowlist_leaves_private_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'distributed/run-example'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({'status': 'passed', 'sample': 'PRIVATE',
                'source': '/secret/genome.vcf', 'jobs': {'one': {'status': 'passed', 'host': 'secret-host',
                'records': 999, 'genotype': 'PRIVATE'}, 'two': {'status': 'failed', 'host': 'other'}}}))
            (run / 'verification.json').write_text('{"passed": true, "variant": "PRIVATE"}')
            folder = root / 'public/Watson'
            folder.mkdir(parents=True)
            (folder / 'archive.gz.part').touch()
            result = snapshot(root)
            self.assertEqual(result['run']['completed'], 1)
            self.assertEqual(result['run']['failed'], 1)
            self.assertEqual(result['run']['workers'], 1)
            self.assertTrue(result['run']['verified'])
            self.assertIn('partial download', result['public_downloads'][0]['status'])
            for secret in ('PRIVATE', 'secret-host', '/secret', 'genotype', 'records'):
                self.assertNotIn(secret, json.dumps(result))

    def test_missing_or_incomplete_state_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(snapshot(root)['run'])
            run = root / 'distributed/run-example'
            run.mkdir(parents=True)
            (run / 'state.json').write_text('{')
            self.assertIsNone(snapshot(root)['run'])
