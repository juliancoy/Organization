import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from launcher_agent import browser_request

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
