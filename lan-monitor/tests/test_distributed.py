import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from distributed import DistributedTests, bundle, plan, targets

class DistributedTestsTests(unittest.TestCase):
    def test_stop_is_idempotent_and_targets_only_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DistributedTests(state=folder)
            self.assertFalse(service.stop())
            service.running = True
            service.run = {'id': '20261023T120000Z-1234abcd', 'status': 'running',
                           'hosts': plan([{'host': 'a.local', 'user': 'julian'}])}
            with patch('distributed.threading.Thread') as thread:
                self.assertTrue(service.stop())
                self.assertFalse(service.stop())
                thread.return_value.start.assert_called_once()
            self.assertEqual(service.view()['run']['status'], 'stopping')
            with patch('distributed.subprocess.run') as remote:
                service.stop_hosts(service.run)
                command = remote.call_args.args[0][-1]
                self.assertIn('/20261023T120000Z-1234abcd/STOP', command)
                self.assertIn('docker rm -f orgportal-test-20261023T120000Z-1234abcd-monitor', command)

    def test_inventory_accepts_saved_mdns_accounts_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'accounts.json'
            path.write_text(json.dumps({'accounts': {'host.local': 'julian'}}))
            self.assertEqual(targets(path), [{'host': 'host.local', 'user': 'julian'}])
            for host, user in [('8.8.8.8', 'julian'), ('host.local;id', 'julian'), ('host.local', 'julian;id')]:
                path.write_text(json.dumps({'accounts': {host: user}}))
                with self.assertRaises(ValueError): targets(path)

    def test_every_suite_is_assigned_and_common_checks_run_on_every_host(self):
        hosts = plan([{'host': name + '.local', 'user': 'julian'} for name in ['a', 'b', 'c']])
        for host in hosts:
            self.assertIn('monitor', host['suites']); self.assertIn('map', host['suites'])
        for suite in ['web', 'org-api', 'chat', 'workspace', 'identity', 'identity-python']:
            self.assertEqual(sum(suite in host['suites'] for host in hosts), 1)

    def test_snapshot_includes_dirty_code_and_excludes_credentials_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as output:
            root = Path(folder)
            for name in ['.', 'OrgPortal', 'PIdP']:
                path = root / name; path.mkdir(exist_ok=True)
                subprocess.run(['git', 'init', '-q', str(path)], check=True)
                for filename,text in [('code.py','original'),('.env','private credential'),('private.pem','private key')]:
                    (path / filename).write_text(text)
                subprocess.run(['git', '-C', str(path), 'add', 'code.py', '.env', 'private.pem'], check=True)
                subprocess.run(['git', '-C', str(path), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'fixture'], check=True)
                (path / 'code.py').write_text('changed code')
            shutil.copyfile(Path(__file__).resolve().parents[2] / 'vm_runner.py', root / 'vm_runner.py')
            (root / 'escape.py').symlink_to('/etc/passwd')
            manifest = {'id': 'test', 'hosts': [], 'repositories': {}}
            archive = Path(output) / 'bundle.tgz'; bundle(root, archive, manifest)
            with tarfile.open(archive) as saved:
                names = saved.getnames()
                self.assertNotIn('.env', names); self.assertNotIn('private.pem', names); self.assertNotIn('escape.py', names)
                self.assertEqual(saved.extractfile('OrgPortal/code.py').read(), b'changed code')
            self.assertEqual(len(manifest['source_sha256']), 64)
            self.assertTrue(manifest['repositories']['OrgPortal']['dirty'])

    def test_restart_marks_jobs_interrupted_and_history_ids_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '20261023T120000Z-1234abcd.json'
            path.write_text(json.dumps({'id': path.stem, 'status': 'running', 'hosts': [{'status': 'running', 'jobs': {'a': {'status': 'running'}}}]}))
            service = DistributedTests(state=folder)
            self.assertEqual(service.view()['run']['hosts'][0]['jobs']['a']['status'], 'interrupted')
            with self.assertRaises(ValueError): service.view('../../etc/passwd')

if __name__ == '__main__': unittest.main()
