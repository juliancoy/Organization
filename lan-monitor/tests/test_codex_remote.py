import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock
from http.server import ThreadingHTTPServer
from codex_remote import Handler,LocalPairingHandler
import codex_devices
import codex_context

class RemoteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name)/'token';p.write_text('owner'*16)
        env=patch.dict(os.environ,{'MAP_CODEX_TOKEN_FILE':str(p)});env.start();self.addCleanup(env.stop)
        device_path=patch.object(codex_devices,'FILE',Path(self.temp.name)/'devices.json');device_path.start();self.addCleanup(device_path.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.public_origin='https://compai.example'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,path,method='GET',token=None,origin=None,host='compai.example',body=None,cookie=None):
        c=http.client.HTTPConnection(*self.server.server_address);headers={'Host':host}
        if cookie:headers['Cookie']=cookie
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        c.request(method,path,body=body,headers=headers);r=c.getresponse();status=r.status;self.last_cookie=r.getheader('Set-Cookie');r.read();c.close();return status
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

    def test_pairing_cookie_reloads_without_owner_token_and_logout_revokes_it(self):
        for bad in [None,False,0,{},[],""]:
            self.assertEqual(self.request('/pair','POST',origin='https://compai.example',body={'token':bad}),400)
            self.assertIsNone(self.last_cookie)
        pair=codex_devices.issue()
        self.assertEqual(self.request('/pair','POST',origin='https://evil.example',body={'token':pair}),403)
        self.assertEqual(self.request('/pair','POST',origin='https://compai.example',body={'token':pair}),200)
        cookie=self.last_cookie
        self.assertIn('Secure; HttpOnly; SameSite=Strict',cookie)
        self.assertTrue(cookie.startswith('__Host-compai_device='))
        cookie=cookie.split(';')[0]
        self.assertEqual(self.request('/pair','POST',origin='https://compai.example',body={'token':pair}),400)
        self.assertEqual(self.request('/logout','POST',origin='https://compai.example',body={}),200)
        self.assertTrue(codex_devices.valid(cookie.split('=',1)[1]))
        with patch('codex_remote.codex_context.rest',return_value=(200,{'threads':[]})):
            self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=cookie),200)
        self.assertEqual(self.request('/logout','POST',origin='https://compai.example',body={},cookie=cookie),200)
        self.assertEqual(self.request('/api/v1/master/codex/threads',cookie=cookie),401)

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

class LocalIssuerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        key=Path(self.temp.name)/'issuer-token';key.write_text('issuer'*16)
        p=patch.dict(os.environ,{'MAP_PAIRING_TOKEN_FILE':str(key)});p.start();self.addCleanup(p.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),LocalPairingHandler);self.server.public_origin='https://compai.example'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,token=None,origin=None,host='127.0.0.1:8092',body=None):
        c=http.client.HTTPConnection(*self.server.server_address);headers={'Host':host,'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        if origin:headers['Origin']=origin
        c.request('POST','/pairing',body=json.dumps({} if body is None else body),headers=headers)
        r=c.getresponse();status=r.status;r.read();c.close();return status
    def test_loopback_and_fleet_credentials_do_not_grant_pairing(self):
        with patch('codex_remote.pairing_payload') as issue:
            self.assertEqual(self.request(),401)
            self.assertEqual(self.request('master'*16),401)
            self.assertEqual(self.request('launcher'*16),401)
            self.assertEqual(self.request('issuer'*16,origin='http://127.0.0.1:8791'),403)
            self.assertEqual(self.request('issuer'*16,host='evil.example'),403)
            self.assertEqual(self.request('issuer'*16,body={'command':'x'}),400)
            issue.assert_not_called()
    def test_scoped_native_request_issues_code(self):
        with patch('codex_remote.pairing_payload',return_value={'url':'https://compai.example/#pair=synthetic'}) as issue:
            self.assertEqual(self.request('issuer'*16),200)
            issue.assert_called_once_with('https://compai.example')
