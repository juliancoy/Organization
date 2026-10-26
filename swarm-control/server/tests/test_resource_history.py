import unittest
from unittest.mock import patch
from resource_history import ResourceHistory
from cpu import CpuMonitor


class ResourceHistoryTests(unittest.TestCase):
    def test_samples_exist_without_any_http_or_view_request(self):
        monitor = CpuMonitor()
        sample = {'host':'compai.local','status':'live','sampled_at':100,'percent':42,'cpus':8}
        with patch('cpu.targets',return_value=[]), patch.object(monitor,'sample',return_value=sample), \
             patch('cpu.time.time',return_value=100), patch('cpu.time.sleep',side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):monitor.worker()
        self.assertEqual(monitor.history.view(100)[0]['hosts'][0]['percent'],42)

    def test_retention_bounds_and_copies_exclude_private_fields(self):
        history = ResourceHistory()
        host={'host':'worker','percent':42,'secret':'private','hardware':{'storage':{'path':'private'},'gpus':[{'name':'GPU','utilization_percent':12,'secret':'private'}]}}
        for now in range(400):history.append([host],now)
        self.assertEqual(len(history.view(399)),61)
        saved=history.view(399)
        self.assertNotIn('private',str(saved))
        saved[-1]['hosts'][0]['percent']=0
        self.assertEqual(history.view(399)[-1]['hosts'][0]['percent'],42)
        self.assertEqual(history.view(1000),[])


    def test_gateway_samples_are_collected_without_browser_and_private_flow_data_is_excluded(self):
        monitor=CpuMonitor(network=lambda:{'devices':[{'ip':'10.42.0.2','mac':'aa','secret':'private'}],
            'link_traffic':{'updated':'now','peers':{'aa':{'rx_mbps':3,'tx_mbps':2,'secret':'private'}},'flows':{'payload':'private'}}})
        with patch('cpu.targets',return_value=[]),patch.object(monitor,'sample',return_value={'host':'compai.local'}), \
             patch('cpu.time.time',return_value=100),patch('cpu.time.sleep',side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):monitor.worker()
        frames=monitor.history.view(100)
        self.assertEqual(frames[0]['network']['link_traffic']['peers']['aa']['rx_mbps'],3)
        self.assertNotIn('private',str(frames))
