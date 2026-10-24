import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock
from http.server import ThreadingHTTPServer
from codex_remote import Handler
import codex_context

class RemoteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name)/'token';p.write_text('owner'*16)
        env=patch.dict(os.environ,{'MAP_CODEX_TOKEN_FILE':str(p)});env.start();self.addCleanup(env.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.public_origin='https://compai.example'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,path,method='GET',token=None,origin=None,host='compai.example',body=None):
        c=http.client.HTTPConnection(*self.server.server_address);headers={'Host':host}
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        c.request(method,path,body=body,headers=headers);r=c.getresponse();status=r.status;r.read();c.close();return status
    def test_private_data_and_commands_require_owner_credentials(self):
        with patch('codex_remote.codex_context.rest') as rest:
            for path in ['/api/v1/master/codex/threads','/api/v1/master/codex/commands']:
                self.assertEqual(self.request(path,'POST' if path.endswith('commands') else 'GET'),401)
            self.assertEqual(self.request('/api/v1/master/status',token='owner'*16),404)
            self.assertEqual(self.request('/../../.env',token='owner'*16),404)
            rest.assert_not_called()
    def test_authenticated_command_is_bound_to_public_origin(self):
        with patch('codex_remote.codex_context.rest',return_value=(202,{'accepted':True})) as rest:
            path='/api/v1/master/codex/commands'
            for origin in [None,'https://evil.example']:
                self.assertEqual(self.request(path,'POST','owner'*16,origin,body={'text':'x'}),403)
            rest.assert_not_called()
            self.assertEqual(self.request(path,'POST','owner'*16,'https://compai.example',body={'text':'x'}),202)
            rest.assert_called_once()
    def test_rebinding_host_is_denied_and_public_page_has_no_credentials(self):
        self.assertEqual(self.request('/',host='evil.example'),403)
        self.assertEqual(self.request('/'),200)

class CommandDeliveryTests(unittest.TestCase):
    def test_new_message_carries_target_thread_id(self):
        ident='11111111-1111-1111-1111-111111111111'
        client=Mock();client.request.return_value={'result':{}}
        with patch('codex_context.thread',return_value={'id':ident}),patch('codex_context.history',return_value={'activity':'idle'}),patch('codex_context.IPC') as ipc,patch.dict(codex_context.RECEIPTS,{},clear=True):
            ipc.return_value.__enter__.return_value=client
            result=codex_context.mutate({'thread_id':ident,'action':'send','text':'Connectivity check','request_id':'22222222-2222-2222-2222-222222222222'})
            self.assertTrue(result['accepted'])
            params=client.request.call_args.args[1]
            self.assertEqual(params['turnStart']['request']['threadId'],params['conversationId'])
