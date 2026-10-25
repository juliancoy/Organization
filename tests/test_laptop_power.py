import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('laptop_power', Path(__file__).resolve().parents[1] / 'scripts/laptop_power.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LaptopPowerTests(unittest.TestCase):
    def state(self, battery=True, closed=True, percent=80):
        return {'on_battery': battery, 'lid_closed': closed, 'battery_percent': percent}

    def test_ac_never_starts_a_lid_timer_even_with_low_battery(self):
        policy = module.Policy()
        self.assertEqual(policy.step(self.state(battery=False, percent=5), 0)['action'], 'awake')
        self.assertEqual(policy.step(self.state(battery=False), 5000)['action'], 'awake')

    def test_closed_battery_gets_twenty_minutes_not_idle_time(self):
        policy = module.Policy()
        self.assertEqual(policy.step(self.state(closed=False), 0)['action'], 'awake')
        self.assertEqual(policy.step(self.state(), 10000)['remaining_seconds'], 1200)
        self.assertEqual(policy.step(self.state(), 11199)['action'], 'draining')
        self.assertEqual(policy.step(self.state(), 11200)['action'], 'suspend')

    def test_reopening_or_ac_cancels_and_next_closure_starts_fresh(self):
        for changed in [self.state(closed=False), self.state(battery=False)]:
            policy = module.Policy()
            policy.step(self.state(), 0)
            self.assertEqual(policy.step(changed, 1000)['action'], 'awake')
            self.assertEqual(policy.step(self.state(), 2000)['remaining_seconds'], 1200)

    def test_low_battery_suspends_without_waiting_for_lid_timer(self):
        self.assertEqual(module.Policy().step(self.state(closed=False, percent=10), 0)['action'], 'suspend')

    def test_real_sensor_format_and_ac_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            battery = root / 'power/BAT0'; battery.mkdir(parents=True)
            for key, value in {'type':'Battery', 'capacity':'80', 'status':'Discharging'}.items():
                (battery / key).write_text(value)
            ac = root / 'power/AC'; ac.mkdir()
            (ac / 'type').write_text('Mains'); (ac / 'online').write_text('0')
            lid = root / 'lid/LID0'; lid.mkdir(parents=True)
            (lid / 'state').write_text('state:      closed\n')
            self.assertTrue(module.sample(root / 'power', root / 'lid')['on_battery'])
            self.assertTrue(module.sample(root / 'power', root / 'lid')['lid_closed'])
            (ac / 'online').write_text('1')
            self.assertFalse(module.sample(root / 'power', root / 'lid')['on_battery'])
