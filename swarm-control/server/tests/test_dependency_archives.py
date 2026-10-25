import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from distributed_worker import prepare_package,dependency_cache

class DependencyArchiveTests(unittest.TestCase):
    def setup_work(self,root,name,lock='one'):
        work=root/name/'source';cwd=work/'OrgPortal/web';cwd.mkdir(parents=True)
        (cwd/'package-lock.json').write_text(json.dumps({'packages':{},'fixture':lock}))
        (cwd/'package.json').write_text('{}')
        return work

    def test_install_is_reused_in_private_copies_and_metadata_is_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);one=self.setup_work(root,'one');two=self.setup_work(root,'two')
            spec={'cwd':'OrgPortal/web','image':'sha256:fixture','prepare':['sh','-c','npm ci --no-audit && npm run prebuild']}
            calls=[]
            def runner(work,task,phase,setup,*args,**kw):
                calls.append(setup['prepare'])
                if setup['prepare'][2].startswith('npm ci'):
                    node=work/'OrgPortal/web/node_modules';node.mkdir();(node/'fixture').write_text('installed')
                return 0
            with patch('distributed_worker.Path.home',return_value=root/'home'),patch('distributed_worker.emit'):
                self.assertEqual(prepare_package(one,'web','web',spec,'one',{},runner),0)
                self.assertEqual(prepare_package(two,'web','web',spec,'two',{},runner),0)
            self.assertEqual(calls,[spec['prepare'],['sh','-c','npm run prebuild']])
            (two/'OrgPortal/web/node_modules/fixture').write_text('modified')
            self.assertEqual((one/'OrgPortal/web/node_modules/fixture').read_text(),'installed')

    def test_lock_runtime_and_command_changes_get_distinct_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['npm','ci']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                keys={dependency_cache(work,'web',spec),dependency_cache(work,'web',{**spec,'image':'sha256:b'}),dependency_cache(work,'web',{**spec,'prepare':['npm','install']})}
                (work/'OrgPortal/web/package-lock.json').write_text('{}')
                keys.add(dependency_cache(work,'web',spec))
            self.assertEqual(len(keys),4)

    def test_corrupt_archive_is_reinstalled_and_failed_installs_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one')
            spec={'cwd':'OrgPortal/web','image':'sha256:fixture','prepare':['sh','-c','npm ci --no-audit']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                cache=dependency_cache(work,'web',spec);archive=cache/'installed-v1.tar.gz';archive.write_text('broken')
                calls=[]
                def runner(*args,**kwargs):calls.append(args);return 17
                self.assertEqual(prepare_package(work,'web','web',spec,'one',{},runner),17)
                self.assertFalse(archive.exists());self.assertEqual(len(calls),1)

    def test_failed_or_cancelled_jobs_do_not_start_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=self.setup_work(root,'one');(work.parent/'STOP').touch()
            spec={'cwd':'OrgPortal/web','image':'sha256:a','prepare':['sh','-c','npm ci --no-audit']}
            with patch('distributed_worker.Path.home',return_value=root/'home'):
                self.assertEqual(prepare_package(work,'web','web',spec,'one',{},lambda *a,**kw:self.fail('Installer must not run')),130)
