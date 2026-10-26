import unittest
from interface_metrics import InterfaceSampler
from resource_history import project_host


def capture(at,eth=(100,200),wifi=(400,500),index=2):
    return {'monotonic':at,'sampled_at':at,'boot_id':'boot','interfaces':{
        'enp1s0':{'ifindex':index,'state':'up','rx_bytes':eth[0],'tx_bytes':eth[1]},
        'wlp2s0':{'ifindex':3,'state':'down','rx_bytes':wifi[0],'tx_bytes':wifi[1]}}}


class InterfaceMetricsTests(unittest.TestCase):
    def test_interfaces_and_directions_are_separate_and_idle_is_zero(self):
        values=iter([capture(1),capture(6,eth=(1000100,2000200))])
        sampler=InterfaceSampler(lambda _:next(values))
        first=sampler.sample({})
        self.assertEqual(first[0]['status'],'warming')
        second=sampler.sample({})
        self.assertEqual(second[0]['rx_mbps'],1.6)
        self.assertEqual(second[0]['tx_mbps'],3.2)
        self.assertEqual(second[1]['network_mbps'],0)

    def test_replaced_interface_and_reset_counters_warm_up(self):
        for after in [capture(6,index=4),capture(6,eth=(0,0))]:
            with self.subTest(after=after):
                values=iter([capture(1),after]);sampler=InterfaceSampler(lambda _:next(values));sampler.sample({})
                self.assertEqual(sampler.sample({})[0]['status'],'warming')

    def test_interface_history_is_bounded_and_does_not_retain_raw_counters(self):
        row=project_host({'host':'worker','network_interfaces':[{'name':'eth0','rx_mbps':2,'secret':'private','rx_bytes':123}]*40})
        self.assertEqual(len(row['network_interfaces']),32)
        self.assertNotIn('private',str(row));self.assertNotIn('rx_bytes',str(row))
