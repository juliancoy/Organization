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
        self.assertIsNone(second[1]['network_mbps'])
        self.assertEqual(second[1]['status'],'disconnected')

    def test_replaced_interface_and_reset_counters_warm_up(self):
        for after in [capture(6,index=4),capture(6,eth=(0,0))]:
            with self.subTest(after=after):
                values=iter([capture(1),after]);sampler=InterfaceSampler(lambda _:next(values));sampler.sample({})
                self.assertEqual(sampler.sample({})[0]['status'],'warming')

    def test_interface_history_is_bounded_and_does_not_retain_raw_counters(self):
        row=project_host({'host':'worker','network_interfaces':[{'name':'eth0','rx_mbps':2,'secret':'private','rx_bytes':123}]*40})
        self.assertEqual(len(row['network_interfaces']),32)
        self.assertNotIn('private',str(row));self.assertNotIn('rx_bytes',str(row))

    def test_removed_observed_interface_is_disconnected_and_reconnects(self):
        missing=capture(6);missing['interfaces'].pop('enp1s0')
        values=iter([capture(1),missing,capture(11),capture(16)])
        sampler=InterfaceSampler(lambda _:next(values));sampler.sample({})
        rows={r['name']:r for r in sampler.sample({})}
        self.assertEqual(rows['enp1s0']['status'],'disconnected')
        self.assertEqual(rows['enp1s0']['state'],'notpresent')
        self.assertIsNone(rows['enp1s0']['network_mbps'])
        self.assertEqual(sampler.sample({})[0]['status'],'warming')
        self.assertEqual(sampler.sample({})[0]['status'],'live')

    def test_failed_capture_does_not_claim_disconnection(self):
        def failed(_):raise OSError('offline')
        self.assertEqual(InterfaceSampler(failed).sample({}),[])

    def test_negotiated_rates_are_projected_without_becoming_throughput(self):
        data=capture(1);data['interfaces']['enp1s0'].update(link_speed_mbps=100,link_duplex='full')
        data['interfaces']['wlp2s0'].update(link_rx_mbps=72.2,link_tx_mbps=65)
        rows=InterfaceSampler(lambda _:data).sample({})
        self.assertEqual(rows[0]['link_speed_mbps'],100)
        self.assertIsNone(rows[0]['network_mbps'])
        projected=project_host({'host':'worker','network_interfaces':rows})['network_interfaces']
        self.assertEqual(projected[0]['link_duplex'],'full')
        self.assertEqual(projected[1]['link_rx_mbps'],72.2)
        self.assertEqual(projected[1]['link_tx_mbps'],65)

    def test_unknown_or_invalid_link_speed_does_not_become_a_capacity(self):
        data=capture(1);data['interfaces']['enp1s0'].update(link_speed_mbps=-1,link_rx_mbps=float('inf'),link_tx_mbps=True)
        row=InterfaceSampler(lambda _:data).sample({})[0]
        self.assertNotIn('link_speed_mbps',row)
        self.assertNotIn('link_rx_mbps',row)
        self.assertNotIn('link_tx_mbps',row)
