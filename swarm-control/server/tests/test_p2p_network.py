import copy
import unittest
import threading
from unittest.mock import Mock
from p2p_network import direct_evidence, NetworkP2P
from distributed import plan
from distributed_images import required_images


class NetworkEvidenceTests(unittest.TestCase):
    def test_cleanup_removes_owned_containers_concurrently_even_if_one_fails(self):
        network=NetworkP2P([], 'run')
        barrier=threading.Barrier(2)
        def stop(*args,**kwargs):barrier.wait(timeout=2)
        healthy=Mock();healthy.call.side_effect=stop
        def offline(*args,**kwargs):
            barrier.wait(timeout=2)
            raise OSError('offline')
        dead=Mock();dead.call.side_effect=offline
        browsers=[Mock(),Mock()]
        network.resources=[{'host':{'host':name},'client':client,'browser':browser}
                           for name,client,browser in zip(['healthy','dead'],[healthy,dead],browsers)]
        network.cleanup()
        self.assertEqual(network.report['cleanup_errors'],['dead'])
        for client,browser in zip([healthy,dead],browsers):
            client.call.assert_called_once_with('p2p.stop',{'id':'run'},timeout=10)
            browser.close.assert_not_called()

    def fixture(self):
        participants=[{'host':'a.local','machine':'machine-a','peer':'a','addresses':['10.42.0.10']},
                      {'host':'b.local','machine':'machine-b','peer':'b','addresses':['10.42.0.11']}]
        links=[{'peer':'b','state':'connected','channels':[{'bytesSent':1200000,'bytesReceived':50}],
                'pair':{'state':'succeeded','bytesSent':1200000,'bytesReceived':50,
                        'local':{'address':'10.42.0.10','candidateType':'host','protocol':'udp'},
                        'remote':{'address':'10.42.0.11','candidateType':'host','protocol':'udp'}}}]
        return participants,links

    def test_real_attested_network_path_is_accepted(self):
        participants,links=self.fixture()
        self.assertEqual(direct_evidence(links,participants,'a.local'),links)

    def test_relay_loopback_hidden_addresses_wrong_hosts_and_no_traffic_cannot_pass(self):
        original,original_links=self.fixture()
        cases=[('remote','candidateType','relay'),('local','address','127.0.0.1'),
               ('remote','address','::1'),('remote','address','hidden.local'),
               ('remote','address','10.42.0.99'),('local','candidateType',None)]
        for candidate,key,value in cases:
            links=copy.deepcopy(original_links);links[0]['pair'][candidate][key]=value
            with self.subTest(candidate=candidate,key=key,value=value),self.assertRaises(RuntimeError):
                direct_evidence(links,original,'a.local')
        links=copy.deepcopy(original_links);links[0]['pair'].update(bytesSent=0,bytesReceived=0)
        with self.assertRaises(RuntimeError):direct_evidence(links,original,'a.local')
        participants=copy.deepcopy(original);participants[1]['machine']=participants[0]['machine']
        with self.assertRaises(RuntimeError):direct_evidence(original_links,participants,'a.local')
        with self.assertRaises(RuntimeError):direct_evidence([],original,'a.local')
        with self.assertRaises(RuntimeError):direct_evidence(original_links,original[:1],'a.local')

    def test_network_suite_is_group_coordinated_and_its_image_is_distributed(self):
        for host in plan([{'host':'a.local','user':'julian'},{'host':'b.local','user':'julian'}]):
            self.assertIn('p2p',host['suites'])
            self.assertIn('selenium/standalone-chrome:latest',required_images(host))


if __name__=='__main__':unittest.main()
