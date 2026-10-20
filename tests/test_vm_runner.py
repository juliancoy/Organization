import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vm_runner', ROOT / 'vm_runner.py')
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)
spec = importlib.util.spec_from_file_location('workspace_run', ROOT / 'run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_uses_current_source_excludes_state_secrets_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'private.txt'
            outside.write_text('outside secret')
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / '.gitignore').write_text('.local/\nignored.txt\n')
            (repo / 'tracked.py').write_text('old version')
            subprocess.run(['git', '-C', str(repo), 'add', 'tracked.py'], check=True)
            (repo / 'tracked.py').write_text('current version')
            (repo / 'new.py').write_text('new source')
            (repo / 'ignored.txt').write_text('private')
            (repo / '.local').mkdir()
            (repo / '.local' / 'accounts.json').write_text('private')
            (repo / '.env').write_text('private')
            (repo / 'pidp_editme.py').write_text('private')
            (repo / '.env.example').write_text('safe template')
            (repo / 'link.py').symlink_to(outside)
            # Even explicitly tracked secrets must not be transferred.
            subprocess.run(['git', '-C', str(repo), 'add', '.env', 'pidp_editme.py', 'link.py'], check=True)
            archive = root / 'bundle.tgz'
            with patch.object(vm, 'REPOSITORIES', ('repo',)):
                vm.snapshot(root, archive)
            with tarfile.open(archive) as bundle:
                names = set(bundle.getnames())
                self.assertEqual(names, {'repo/.gitignore', 'repo/tracked.py', 'repo/new.py', 'repo/.env.example'})
                self.assertEqual(bundle.extractfile('repo/tracked.py').read(), b'current version')


class LifecycleTests(unittest.TestCase):
    def exercise(self, failure=None, cleanup_failure=False):
        args = SimpleNamespace(vm_timeout=3600, vm_cpus=2, vm_memory='4G', vm_disk='30G')
        calls = []

        def execute(command, **kwargs):
            calls.append(command)
            if command[1] == 'delete':
                return SimpleNamespace(returncode=int(cleanup_failure))
            if failure == 'launch' and command[1] == 'launch':
                raise subprocess.CalledProcessError(1, command)
            if command[1] == 'exec' and command[-1].endswith('guest_tests.py'):
                if failure == 'test':
                    raise subprocess.CalledProcessError(1, command)
                if failure == 'interrupt':
                    raise KeyboardInterrupt
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(command, 3600)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory, patch.object(vm.shutil, 'which', return_value='/bin/multipass'), \
                patch.object(vm, 'snapshot'), patch.object(vm.subprocess, 'run', side_effect=execute), \
                contextlib.redirect_stdout(io.StringIO()):
            result = vm.run_vm(Path(directory), args)
        return result, calls

    def test_success_collects_reports_then_deletes_only_owned_vm(self):
        status, calls = self.exercise()
        self.assertEqual(status, 0)
        name = calls[0][calls[0].index('--name') + 1]
        self.assertTrue(name.startswith('organization-test-'))
        self.assertEqual(calls[-1], ['multipass', 'delete', '--purge', name])
        self.assertEqual(calls[-2][1:3], ['transfer', '--recursive'])
        self.assertFalse(any('mount' in call or '--all' in call for call in calls))

    def test_failed_launch_test_timeout_and_interrupt_always_delete_vm(self):
        for failure in ['launch', 'test', 'timeout', 'interrupt']:
            with self.subTest(failure=failure):
                status, calls = self.exercise(failure)
                self.assertNotEqual(status, 0)
                self.assertEqual(calls[-1][1:3], ['delete', '--purge'])

    def test_cleanup_failure_cannot_report_success(self):
        self.assertEqual(self.exercise(cleanup_failure=True)[0], 1)

    def test_missing_multipass_does_not_touch_docker_or_create_a_vm(self):
        with patch.object(vm.shutil, 'which', return_value=None), patch.object(vm.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Multipass is required'):
                vm.run_vm(ROOT, SimpleNamespace())
            command.assert_not_called()

    def test_vm_rejects_host_login_and_custom_namespace(self):
        for args in [['--vm', '--login', 'admin'], ['--vm', '--prefix', 'other-'], ['--vm', '--port', '9443']]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                launcher.parse_args(args)
        self.assertTrue(launcher.parse_args(['--vm']).vm)


if __name__ == '__main__':
    unittest.main()
