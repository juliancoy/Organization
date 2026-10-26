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
