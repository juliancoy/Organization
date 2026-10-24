#!/usr/bin/env python3
"""Remove expired production working copies hourly without remote credentials."""
from pathlib import Path
import shutil
import subprocess

from setup_recovery_timer import quote

ROOT = Path(__file__).resolve().parents[1]


def main():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js 22 or newer is required')
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    service = f'''[Unit]
Description=Remove expired production organization working copies

[Service]
Type=oneshot
WorkingDirectory={ROOT / 'OrgPortal'}
ExecStart={quote(node)} {quote(ROOT / 'OrgPortal/org-worker/scripts/orgportal.mjs')} working-copy purge
UMask=0077
TimeoutStartSec=2m
'''
    timer = '''[Unit]
Description=Hourly organization working-copy expiry

[Timer]
OnCalendar=hourly
Persistent=true
Unit=organization-working-copy-expiry.service

[Install]
WantedBy=timers.target
'''
    for name, content in [('organization-working-copy-expiry.service', service), ('organization-working-copy-expiry.timer', timer)]:
        path = units / name
        path.write_text(content)
        path.chmod(0o600)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'organization-working-copy-expiry.timer'], check=True)
    print('Hourly production working-copy expiry enabled.')


if __name__ == '__main__':
    main()
