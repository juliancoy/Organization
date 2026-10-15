import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)


class PullAllTest(unittest.TestCase):
    def test_clone_update_and_continue_after_divergence(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            scripts, target, source = [base / name for name in ('scripts', 'target', 'source')]
            for path in (scripts, target, source):
                path.mkdir()
            shutil.copy(ROOT / 'pull-all.sh', scripts)
            git('init', '-b', 'main', source)
            git('-C', source, 'config', 'user.email', 'test@example.com')
            git('-C', source, 'config', 'user.name', 'Test')
            (source / 'file').write_text('initial')
            git('-C', source, 'add', '.')
            git('-C', source, 'commit', '-m', 'Initial')
            remote = base / 'remote.git'
            git('clone', '--bare', source, remote)
            git('-C', source, 'remote', 'add', 'origin', remote)
            (scripts / 'repositories.txt').write_text(f'first {remote}\nsecond {remote}\n')

            def pull():
                return subprocess.run(['bash', str(scripts / 'pull-all.sh'), str(target)], capture_output=True, text=True)

            self.assertEqual(pull().returncode, 0)
            self.assertTrue((target / 'first' / '.git').is_dir())
            first = target / 'first'
            git('-C', first, 'config', 'user.email', 'test@example.com')
            git('-C', first, 'config', 'user.name', 'Test')
            (first / 'local').write_text('preserve me')
            git('-C', first, 'add', '.')
            git('-C', first, 'commit', '-m', 'Local work')
            local_head = git('-C', first, 'rev-parse', 'HEAD').stdout
            (source / 'file').write_text('upstream update')
            git('-C', source, 'commit', '-am', 'Upstream')
            git('-C', source, 'push', 'origin', 'main')
            result = pull()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Repositories: 2; successful: 1; failed: 1', result.stdout)
            self.assertEqual(git('-C', first, 'rev-parse', 'HEAD').stdout, local_head)
            self.assertEqual((target / 'second' / 'file').read_text(), 'upstream update')
            self.assertEqual((first / 'local').read_text(), 'preserve me')

    def test_existing_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            shutil.copy(ROOT / 'pull-all.sh', base)
            (base / 'repositories.txt').write_text('occupied https://example.invalid/repo.git\n')
            (base / 'occupied').mkdir()
            marker = base / 'occupied' / 'important'
            marker.write_text('keep')
            result = subprocess.run(['bash', str(base / 'pull-all.sh')], capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(marker.read_text(), 'keep')
