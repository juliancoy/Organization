from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from agent_updates import LauncherUpdates


class LauncherUpdateTests(unittest.TestCase):
    def setUp(self):
        self.progress=Mock()
        self.updates=LauncherUpdates(Path(__file__).resolve().parents[2],lambda:False,self.progress)
        self.host={'host':'worker','lan_address':'10.42.0.2'}
        self.status={'node':'worker','machine':'machine','ready':True,'source_revision':'old','active_jobs':[]}

    def test_current_launcher_does_not_reload(self):
        status={**self.status,'source_revision':self.updates.revision}
        with patch.object(self.updates,'stage') as stage:
            self.assertIs(self.updates.ensure(self.host,Mock(),status),status)
        stage.assert_not_called()

    def test_verified_ipv4_endpoint_overrides_an_ambient_ipv6_only_ssh_setting(self):
        with patch('agent_updates.subprocess.run',return_value=Mock(returncode=0)) as run, patch('agent_client.CONFIG') as config:
            config.read_text.return_value='{"controller_public_key":"'+'ab'*32+'"}'
            self.updates.stage(self.host,self.status)
        command=run.call_args.args[0]
        self.assertIn('AddressFamily=any',command)
        self.assertIn('HostKeyAlias=worker.local',command)

    def test_outdated_launcher_waits_through_restart_and_checks_revision(self):
        ready={**self.status,'source_revision':self.updates.revision}
        client=Mock();client.call.side_effect=[ConnectionError('restarting'),ready]
        with patch.object(self.updates,'stage') as stage,patch('agent_updates.time.sleep'):
            self.assertEqual(self.updates.ensure(self.host,client,self.status),ready)
        stage.assert_called_once_with(self.host,self.status)
        self.assertEqual(self.host['launcher_update']['status'],'updated')
        self.assertEqual([call.args[1] for call in self.progress.call_args_list],['updating','waiting_for_update'])

    def test_busy_launcher_is_not_updated(self):
        with patch.object(self.updates,'stage') as stage:
            with self.assertRaisesRegex(RuntimeError,'busy'):
                self.updates.ensure(self.host,Mock(),{**self.status,'active_jobs':['existing-run']})
        stage.assert_not_called()

    def test_replacement_machine_cannot_pass_update_readiness(self):
        client=Mock();client.call.return_value={**self.status,'machine':'another','source_revision':self.updates.revision}
        with patch.object(self.updates,'stage'):
            with self.assertRaisesRegex(RuntimeError,'identity changed'):
                self.updates.ensure(self.host,client,self.status)

    def test_cancellation_during_reload_stops_polling(self):
        self.updates.stopped=lambda:True
        client=Mock()
        with patch.object(self.updates,'stage'):
            with self.assertRaises(InterruptedError):self.updates.ensure(self.host,client,self.status)
        client.call.assert_not_called()

    def test_unready_replacement_times_out_without_admitting_worker(self):
        with patch.object(self.updates,'stage'),patch('agent_updates.time.monotonic',side_effect=[0,121]):
            with self.assertRaisesRegex(RuntimeError,'did not become ready'):
                self.updates.ensure(self.host,Mock(),self.status)
