"""Local laptop policy: closed on battery for 20 minutes, then suspend."""
import json
from pathlib import Path
import subprocess
import time

DELAY = 20 * 60
LOW_BATTERY = 10


def sample(power_root=Path('/sys/class/power_supply'), lid_root=Path('/proc/acpi/button/lid')):
    batteries, online = [], []
    for device in power_root.iterdir():
        kind = (device / 'type').read_text().strip()
        if kind == 'Battery':
            if (device / 'scope').exists() and (device / 'scope').read_text().strip() == 'Device':
                continue
            batteries.append({'percent': int((device / 'capacity').read_text()),
                              'status': (device / 'status').read_text().strip()})
        elif (device / 'online').exists():
            online.append((device / 'online').read_text().strip() == '1')
    lids = [p.read_text().strip().split()[-1] for p in lid_root.glob('*/state')]
    return {'on_battery': bool(batteries) and (not any(online) if online else
                any(b['status'] == 'Discharging' for b in batteries)),
            'battery_percent': min((b['percent'] for b in batteries), default=None),
            'lid_closed': bool(lids) and all(state == 'closed' for state in lids)}


class Policy:
    def __init__(self):
        self.closed_since = None

    def step(self, state, now):
        if not state['on_battery']:
            self.closed_since = None
            return {'action': 'awake', 'reason': 'AC power', 'remaining_seconds': None}
        if state['battery_percent'] is not None and state['battery_percent'] <= LOW_BATTERY:
            return {'action': 'suspend', 'reason': 'Battery at or below 10%', 'remaining_seconds': 0}
        if not state['lid_closed']:
            self.closed_since = None
            return {'action': 'awake', 'reason': 'Lid open on battery', 'remaining_seconds': None}
        if self.closed_since is None:
            self.closed_since = now
        remaining = max(0, DELAY - (now - self.closed_since))
        return {'action': 'suspend' if remaining == 0 else 'draining',
                'reason': 'Lid closed on battery', 'remaining_seconds': round(remaining)}


def main():
    folder = Path('/run/lifetech-power')
    policy = Policy()
    last_action, retry_at = None, 0
    while True:
        now = time.monotonic()
        try:
            state = sample()
            decision = policy.step(state, now)
            record = {**state, **decision, 'updated_at': time.time(), 'delay_seconds': DELAY}
            temporary = folder / 'status.tmp'
            temporary.write_text(json.dumps(record) + '\n')
            temporary.chmod(0o644)
            temporary.replace(folder / 'status.json')
            action = (decision['action'], decision['reason'])
            if action != last_action:
                print(json.dumps(record), flush=True)
                last_action = action
            if decision['action'] == 'suspend' and now >= retry_at:
                # The fixed local command is never supplied by a worker or API.
                subprocess.run(['/usr/bin/systemctl', 'suspend'], check=True, timeout=60)
                policy.closed_since = None
                retry_at = time.monotonic() + 60
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print('Power policy: ' + str(error)[:300], flush=True)
            retry_at = time.monotonic() + 60
        time.sleep(2)


if __name__ == '__main__':
    main()
