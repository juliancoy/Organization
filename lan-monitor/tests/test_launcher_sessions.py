import subprocess
import unittest
from unittest.mock import Mock,patch
from launcher_sessions import LauncherSessions


class SessionPreparationTests(unittest.TestCase):
    def service(self):
        hosts=[{'host':'a.local','user':'a'},{'host':'b.local','user':'b'}]
        service=LauncherSessions('.',hosts,lambda host:['ssh',host['host']],Mock(),lambda:False,Mock())
        return service,hosts

    def test_unreachable_machine_is_reported_and_reachable_machine_is_kept(self):
        service,hosts=self.service()
        def command(host,*args,**kwargs):
            if host['host']=='b.local':raise subprocess.TimeoutExpired('ssh',45)
            return Mock(returncode=0,stdout='3\nlinux\n')
        with patch.object(service,'command',side_effect=command):
            self.assertEqual(service.accessible(),[hosts[0]])
        self.assertEqual(hosts[1]['session']['status'],'unavailable')

    def test_web_rtc_verification_requires_every_peer(self):
        service,hosts=self.service()
        for host in hosts:host['session']={'container':'lifetech-launcher-session','status':'ready'}
        with patch.object(service,'command',return_value=Mock(returncode=0)) as command:
            service.verify(hosts)
        self.assertEqual(command.call_count,2)
        self.assertTrue(all(host['session']['verified'] for host in hosts))
        self.assertIn('received',command.call_args.args[1])
        self.assertIn("connection",command.call_args.args[1])

    def test_cancellation_does_not_start_a_remote_command(self):
        service,hosts=self.service();service.stopped=lambda:True
        with patch('launcher_sessions.subprocess.run') as run:
            with self.assertRaises(InterruptedError):service.command(hosts[0],'docker info')
        run.assert_not_called()
