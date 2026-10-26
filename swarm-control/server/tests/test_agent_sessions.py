import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from agent_client import AgentSessions


class AgentReadinessTests(unittest.TestCase):
    def test_offline_node_does_not_exclude_healthy_agent(self):
        root=Path(__file__).resolve().parents[3]
        hosts=[{'host':'a.local','lan_address':'10.42.0.1'},{'host':'b.local','lan_address':'10.42.0.2'}]
        folder=root/'scripts'
        revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        status={'protocol':'lifetech-agent.v1','ready':True,'source_revision':revision,
                'capabilities':['github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources','controller-signature'],
                'addresses':['10.42.0.1'],'machine':'machine-a','active_jobs':[]}
        def client(host):
            result=Mock()
            if host['host']=='a.local':result.call.return_value=status
            else:result.call.side_effect=TimeoutError('offline')
            return result
        service=AgentSessions(root,hosts,lambda:False,Mock())
        with patch('agent_client.AgentClient',side_effect=client):ready=service.verify(hosts)
        self.assertEqual(ready,[hosts[0]])
        self.assertTrue(hosts[0]['session']['verified'])
        self.assertIn('offline',hosts[1]['error'])

    def test_cancellation_does_not_contact_an_agent(self):
        service=AgentSessions('.',[],lambda:True,Mock())
        with patch('agent_client.AgentClient') as client:
            with self.assertRaises(InterruptedError):service.verify([{'host':'a.local'}])
        client.assert_not_called()
