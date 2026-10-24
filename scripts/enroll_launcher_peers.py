"""Provision one private WebRTC fleet config via saved LAN SSH accounts."""
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import socket


def enroll(root):
    sys.path.insert(0, str(root / 'lan-monitor'))
    from distributed import targets, ssh
    sys.path.insert(0, str(root / 'scripts'))
    from launcher_peers import CONFIG
    hosts = targets()
    peers = {host['host']: socket.gethostbyname(host['host']) for host in hosts}
    local_host=next((host for host in hosts if host.get('local')),None)
    remote=next((host for host in hosts if not host.get('local')),None)
    if local_host and remote:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
            route.connect((peers[remote['host']],8790))
            peers[local_host['host']]=route.getsockname()[0]
    key = (json.loads(CONFIG.read_text()).get('key') if CONFIG.exists() else None) or secrets.token_hex(32)
    if len(bytes.fromhex(key)) != 32:
        raise ValueError('Existing mesh key must contain 32 bytes')
    # Persist locally first, so retrying a partially completed enrollment keeps its key.
    ordered = sorted(hosts, key=lambda host: not host.get('local', False))
    for host in ordered:
        config = {'name': host['host'], 'peers': peers, 'key': key}
        destination = '/home/' + host['user'] + '/.config/lifetech-launcher/peers.json'
        code = '''import pathlib,sys,os,json
os.umask(0o077)
p=pathlib.Path(DESTINATION);p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
incoming=json.loads(sys.stdin.buffer.read());previous=json.loads(p.read_text()) if p.exists() else {};tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(previous|incoming));tmp.chmod(0o600);tmp.replace(p)
'''.replace('DESTINATION', repr(destination))
        subprocess.run(ssh(host) + ['python3 -c ' + shlex.quote(code)],
                       input=json.dumps(config).encode(), check=True, timeout=30)
        print('Enrolled ' + host['host'], flush=True)
    print('Enrollment complete. Recreate launcher containers with ./run.py --recreate-launcher on each machine to activate WebRTC.')
