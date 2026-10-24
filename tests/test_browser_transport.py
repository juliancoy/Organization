import json
from pathlib import Path
import sys
import unittest
import tempfile
import threading
from unittest.mock import Mock,MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from launcher_agent import browser_request,Agent

class BrowserTransportTests(unittest.TestCase):
    def test_requests_use_the_owned_container_namespace_without_a_shell(self):
        container=Mock();container.exec_run.return_value=(0,b'{"value":{"ready":true}}')
        data={'script':'return "$(do-not-execute)";','args':[]}
        self.assertEqual(browser_request(container,'/session/owned/execute/sync',data,'POST'),{'value':{'ready':True}})
        args=container.exec_run.call_args
        self.assertIsInstance(args.args[0],list)
        self.assertEqual(args.args[0][:2],['/usr/bin/python3','-c'])
        self.assertEqual(json.loads(args.args[0][-1]),{'path':'/session/owned/execute/sync','data':data,'method':'POST'})
        self.assertEqual(args.kwargs['user'],'1200:1201')

    def test_exec_failure_is_redacted_and_oversized_responses_fail(self):
        for result in ((1,b'private diagnostic'),(0,b'x'*(4*1024*1024+1))):
            container=Mock();container.exec_run.return_value=result
            with self.assertRaisesRegex(RuntimeError,'Owned browser WebDriver request failed'):
                browser_request(container,'/status',None,'GET')

    def test_private_agent_umask_does_not_make_public_browser_fixture_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent=Agent.__new__(Agent);agent.root=Path(temporary)
            agent.browsers=set();agent.browser_timers={};agent.lock=threading.RLock();agent.busy=Mock()
            def extract(upload,folder):
                folder.mkdir(mode=0o700)
                for name in ('remote.py','peer.js'):(folder/name).write_text('public fixture')
            agent.extract=extract
            client=MagicMock();client.__enter__.return_value=client;agent.client=lambda:client
            image='sha256:'+64*'a';client.images.get.return_value.id=image
            client.containers.run.return_value.id='owned-container'
            ident='20261024T221500Z-1234abcd'
            with patch('launcher_agent.threading.Timer'):
                agent.p2p_start({'id':ident,'image':image,'upload':'fixture'})
            folder=agent.folder(ident+'-p2p')
            self.assertEqual(folder.stat().st_mode & 0o777,0o755)
            self.assertEqual((folder/'remote.py').read_text(),'public fixture')
            self.assertEqual(client.containers.run.call_args.kwargs['user'],'1200:1201')
