import unittest
from cpu import aggregate, CpuMonitor, SAMPLE
import subprocess
import json
from unittest.mock import patch

class CpuTests(unittest.TestCase):
    def test_total_is_capacity_weighted_and_excludes_unavailable_hosts(self):
        self.assertEqual(aggregate([{'percent':100,'cpus':2,'status':'live'},
                                    {'percent':0,'cpus':6,'status':'live'},
                                    {'percent':99,'cpus':32,'status':'stale'}]),25)
        self.assertIsNone(aggregate([]))

    def test_old_samples_do_not_appear_live(self):
        monitor=CpuMonitor()
        monitor.hosts=[{'host':'a','percent':50,'cpus':8,'status':'live','sampled_at':1}]
        with patch('cpu.time.time',return_value=20):
            view=monitor.view()
        self.assertIsNone(view['total_percent'])
        self.assertEqual(view['live_hosts'],0)
        self.assertEqual(view['hosts'][0]['status'],'stale')

    def test_local_resource_sample_reports_real_metrics(self):
        values=json.loads(subprocess.check_output(['python3','-c',SAMPLE],text=True))
        self.assertGreater(values['ram_total_bytes'],0)
        self.assertGreaterEqual(values['ram_percent'],0)
        self.assertLessEqual(values['ram_percent'],100)
        self.assertGreaterEqual(values['network_mbps'],0)
        if values['disk_busy_percent'] is not None:
            self.assertGreaterEqual(values['disk_busy_percent'],0)
            self.assertLessEqual(values['disk_busy_percent'],100)
