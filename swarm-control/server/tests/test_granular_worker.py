import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from granular_worker import Dependencies


class DependencySetupTests(unittest.TestCase):
    def test_concurrent_files_install_a_shared_package_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            setup=Dependencies(Path(folder),{'id':'run'}, {})
            spec={'base':'org-api','image':'immutable-node','cwd':'OrgPortal/org-worker'}
            with patch('granular_worker.run',return_value=0) as run, patch('granular_worker.emit'):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    results=list(pool.map(lambda n:setup.ensure('org-api--'+str(n),spec),range(20)))
                self.assertEqual(results,[0]*20)
                self.assertEqual(run.call_count,2)  # chat dependency and org-worker
                self.assertEqual({call.args[3]['cwd'] for call in run.call_args_list},
                                 {'OrgPortal/chat-worker','OrgPortal/org-worker'})

    def test_failed_install_is_reported_to_every_dependent_file(self):
        with tempfile.TemporaryDirectory() as folder:
            setup=Dependencies(Path(folder),{'id':'run'}, {})
            spec={'base':'identity-python','image':'python','cwd':'PIdP'}
            with patch('granular_worker.run',return_value=17) as run, patch('granular_worker.emit'):
                self.assertEqual(setup.ensure('first',spec),17)
                self.assertEqual(setup.ensure('second',spec),17)
                run.assert_called_once()
