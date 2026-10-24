#!/usr/bin/env python3
"""Install a user-level, read-only daily DR backup and isolated restore drill."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def main():
    units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
    python=shutil.which('python3');node=shutil.which('node')
    if not python or not node:raise RuntimeError('Python and Node must be installed')
    # Node is commonly installed through nvm; systemd does not start a login shell.
    service=f'''[Unit]
Description=Complete Organization local DR copy and isolated restore verification

[Service]
Type=oneshot
WorkingDirectory={ROOT}
ExecStart={quote(python)} {quote(ROOT/'scripts/recovery.py')} backup
ExecStartPost=-{quote(python)} {quote(ROOT/'scripts/testing_copy.py')} --purge-expired
ExecStartPost={quote(python)} {quote(ROOT/'scripts/dev_data.py')} purge
ExecStartPost={quote(python)} {quote(ROOT/'scripts/dev_data.py')} refresh
Environment=PATH={Path(node).parent}:/usr/local/bin:/usr/bin:/bin
Environment=CI=true
UMask=0077
TimeoutStartSec=12h
'''
    timer='''[Unit]
Description=Daily Organization DR copy

[Timer]
OnCalendar=*-*-* 03:15:00 America/New_York
Persistent=true
RandomizedDelaySec=5m
Unit=organization-dr.service

[Install]
WantedBy=timers.target
'''
    for name,content in [('organization-dr.service',service),('organization-dr.timer',timer)]:
        p=units/name;p.write_text(content);p.chmod(0o600)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','organization-dr.timer'],check=True)
    print('Daily DR copy and restore verification enabled for 03:15 America/New_York.')


if __name__=='__main__':main()
