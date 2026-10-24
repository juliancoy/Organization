import io
import json
import unittest
from unittest.mock import patch
import test_codex_remote as remote_tests
import codex_devices
import swarm_status


class RemoteStatusBoundaryTests(unittest.TestCase):
    setUp = remote_tests.RemoteBoundaryTests.setUp
    request = remote_tests.RemoteBoundaryTests.request

    def test_status_requires_owner_or_paired_session_and_forbids_writes(self):
        with patch('swarm_status.snapshot', return_value={'devices': [], 'run': {}}) as snapshot:
            self.assertEqual(self.request('/api/v1/swarm/status'), 401)
            self.assertEqual(self.request('/api/v1/swarm/status', 'POST', 'owner'*16, 'https://compai.example', body={}), 404)
            snapshot.assert_not_called()
            self.assertEqual(self.request('/api/v1/swarm/status', token='owner'*16), 200)
            cookie='__Host-compai_device='+codex_devices.owner_session()
            self.assertEqual(self.request('/api/v1/swarm/status', cookie=cookie), 200)
            self.assertEqual(self.request('/api/v1/swarm/status', cookie=cookie, origin='https://evil.example'), 403)

    def test_unavailable_coordinator_returns_retryable_error(self):
        with patch('swarm_status.snapshot', side_effect=OSError('private upstream detail')):
            self.assertEqual(self.request('/api/v1/swarm/status', token='owner'*16), 503)


class SnapshotProjectionTests(unittest.TestCase):
    def test_fixed_read_sources_exclude_private_coordinator_fields(self):
        network={'updated':'2026-10-24T20:00:00Z','devices':[{'hostname':'worker.local','ip':'10.0.0.2','ping':True,'mac':'private','ports':[22]}]}
        tests={'run':{'id':'fixture','status':'running','hosts':[{'host':'worker.local','status':'running','session':{'secret':'private'},'logs':['private'], 'jobs':{'test':{'name':'Fixture test','status':'running','logs':['private']}}}]}}
        with patch('swarm_status.urlopen', side_effect=[io.BytesIO(json.dumps(network).encode()),io.BytesIO(json.dumps(tests).encode())]) as fetch:
            result=swarm_status.snapshot()
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list],['http://127.0.0.1:8089/api/devices','http://127.0.0.1:8089/api/distributed-tests'])
        self.assertNotIn('private',json.dumps(result))
        self.assertTrue(result['devices'][0]['reachable'])
        self.assertEqual(result['run']['hosts'][0]['jobs'][0]['status'],'running')
