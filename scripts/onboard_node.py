"""Enroll one mDNS LAN node and install its supervised Docker test agent."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'organization-launcher:webrtc-1'
FILES = ('launcher_peers.py', 'launcher_discovery.py', 'launcher_registration.py',
         'launcher_agent.py', 'launcher_registry.py', 'launcher_peer_supervisor.py',
         'launcher_update.py')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('node', help='user@hostname.local')
    parser.add_argument('--address', help='SSH address override, including an IPv6 interface scope')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9-]*\.local', args.node):
        parser.error('Use user@hostname.local')
    user, node = args.node.split('@')
    node = node.lower()
    ssh = ['ssh', '-C', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=8', '-o', 'ForwardAgent=no', '-o', 'HostKeyAlias=' + node]
    key = Path.home() / '.ssh/orgportal_lan_ed25519'
    if key.exists():
        ssh += ['-i', str(key)]
    if args.address:
        ssh += ['-o', 'HostName=' + args.address.replace('%', '%%')]
    ssh += [user + '@' + node]

    def remote(code, payload=None, timeout=40):
        return subprocess.run(ssh + ['python3 -c ' + shlex.quote(code)],
                              input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, check=True, timeout=timeout).stdout

    config_path = Path.home() / '.config/lifetech-launcher/peers.json'
    config = json.loads(config_path.read_text())
    probe = json.loads(remote('''import hashlib,json,pathlib,socket,subprocess
print(json.dumps(dict(host=socket.gethostname().lower()+'.local',home=str(pathlib.Path.home()),
 machine=hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest(),
 interfaces=json.loads(subprocess.check_output(['ip','-j','-4','addr'],text=True)),
 docker=json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))['OSType'])))
'''))
    if probe['host'] != node or probe['docker'] != 'linux':
        raise RuntimeError('Remote hostname or Docker platform does not match the requested node')
    rows = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', 'enp7s0'], text=True))
    networks = [ipaddress.ip_network(a['local'] + '/' + str(a['prefixlen']), strict=False)
                for row in rows for a in row.get('addr_info', []) if a.get('scope') == 'global']
    address = next((a['local'] for row in probe['interfaces'] if row['ifname'].startswith(('en', 'eth', 'wl'))
                    for a in row.get('addr_info', []) if a.get('scope') == 'global'
                    and any(ipaddress.ip_address(a['local']) in net for net in networks)), None)
    if not address:
        raise RuntimeError('Node has no physical address on the coordinator Ethernet subnet')
    if node not in config['peers'] and len(config['peers']) >= 24:
        raise RuntimeError('Fleet already has 24 nodes')
    if any(name != node and value == address for name, value in config['peers'].items()):
        raise RuntimeError('Address is already enrolled under another node name')
    print(f'Verified {node} at {address}; Docker Linux available', flush=True)
    # Keep the existing fleet credential and all existing peers.
    config['peers'][node] = address
    destination = probe['home'] + '/.local/share/organization-node'
    remote_config = {**config, 'name': node}
    payload = dict(config=remote_config, folder=destination,
                   files={name: (ROOT / 'scripts' / name).read_text() for name in FILES},
                   public_key=key.with_suffix('.pub').read_text().strip() if key.with_suffix('.pub').exists() else '')
    remote('''import json,os,pathlib,sys
os.umask(0o077)
v=json.load(sys.stdin)
p=pathlib.Path.home()/'.config/lifetech-launcher/peers.json';p.parent.mkdir(parents=True,exist_ok=True)
old=json.loads(p.read_text()) if p.exists() else {}
if old and (old['name']!=v['config']['name'] or old['key']!=v['config']['key']):
 raise RuntimeError('Node already belongs to a different fleet')
root=pathlib.Path(v['folder']);scripts=root/'scripts';scripts.mkdir(parents=True,exist_ok=True)
for name,source in v['files'].items():
 compile(source,name,'exec');(scripts/name).write_text(source)
t=p.with_suffix('.tmp');t.write_text(json.dumps(old|v['config']));t.chmod(0o600);t.replace(p)
if v['public_key']:
 p=pathlib.Path.home()/'.ssh/authorized_keys';p.parent.mkdir(parents=True,exist_ok=True)
 old=p.read_text() if p.exists() else ''
 if v['public_key'] not in old.splitlines():p.write_text(old.rstrip()+'\\n'+v['public_key']+'\\n');p.chmod(0o600)
''', payload)
    image_id = subprocess.check_output(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], text=True).strip()
    present = remote("import subprocess;print(subprocess.run(['docker','image','inspect'," + repr(image_id) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode)").strip() == '0'
    if not present:
        print('Transferring the existing launcher image over SSH', flush=True)
        with subprocess.Popen(['docker', 'image', 'save', IMAGE], stdout=subprocess.PIPE) as producer:
            loaded = subprocess.run(ssh + ['docker image load'], stdin=producer.stdout, timeout=600)
            producer.stdout.close()
            if producer.wait() or loaded.returncode:
                raise RuntimeError('Launcher image transfer failed')
    remote('''import json,os,pathlib,subprocess,sys
v=json.load(sys.stdin);root=v['folder'];name='lifetech-launcher-session'
result=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
if result.returncode==0:
 state=json.loads(result.stdout)[0]
 if state['State']['Running']:print('Existing supervised agent retained');sys.exit(0)
 subprocess.run(['docker','rm',name],check=True,stdout=subprocess.DEVNULL)
jobs=root+'/.runtime/launcher-jobs';pathlib.Path(jobs).mkdir(parents=True,exist_ok=True)
config=str(pathlib.Path.home()/'.config/lifetech-launcher')
command=['docker','run','--detach','--init','--restart','unless-stopped','--name',name,
 '--network','host','--user',str(os.getuid())+':'+str(os.getgid()),
 '--group-add',str(os.stat('/var/run/docker.sock').st_gid),'--pull=never',
 '--log-opt','max-size=5m','--log-opt','max-file=3',
 '--label','organization.container-class=launcher',
 '--mount','type=bind,src='+root+',dst='+root,
 '--mount','type=bind,src='+config+',dst=/run/launcher-config',
 '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
 '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
 '--env','HOME=/tmp','--env','PYTHONUNBUFFERED=1',
 '--env','LIFETECH_PEER_CONFIG=/run/launcher-config/peers.json',
 '--env','LIFETECH_JOB_ROOT='+jobs,'--no-healthcheck',v['image'],
 'python',root+'/scripts/launcher_peer_supervisor.py']
subprocess.run(command,check=True,stdout=subprocess.DEVNULL)
print('Installed supervised test agent')
''', dict(folder=destination, image=image_id))
    save(config_path, config)
    inventory_path = Path.home() / '.local/state/orgportal/lan-ssh-devices.json'
    inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {}
    inventory.setdefault('accounts', {})[node] = user
    save(inventory_path, inventory)
    sys.path.insert(0, str(ROOT / 'lan-monitor'))
    from agent_client import AgentClient
    client = AgentClient(dict(host=node, agent_endpoint='http://' + address + ':8790'))
    expected = hashlib.sha256(''.join(payload['files'][name] for name in FILES[:5]).encode()).hexdigest()
    deadline = time.monotonic() + 60
    try:
        while True:
            try:
                status = client.call('status', timeout=5)
                if status['node'] != node or status['machine'] != probe['machine']:
                    raise RuntimeError('Agent identity does not match the SSH machine')
                if status['ready'] and status['source_revision'] == expected and address in status['addresses']:
                    break
            except (OSError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Agent did not become ready; inspect docker logs lifetech-launcher-session on the node')
            time.sleep(1)
    finally:
        client.close()
    print(f'Enrolled {node}: authenticated agent ready at {address}:8790', flush=True)
    # Mesh membership must also reach existing nodes; offline peers remain enrolled.
    for peer, peer_address in config['peers'].items():
        if peer in (node, config['name']):
            continue
        peer_user = inventory['accounts'].get(peer)
        if not peer_user:
            print(f'Fleet config pending on {peer}: no saved SSH account', flush=True)
            continue
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=5', '-o', 'ForwardAgent=no',
                   '-o', 'HostName=' + peer_address, '-o', 'HostKeyAlias=' + peer]
        if key.exists():
            command += ['-i', str(key)]
        code = '''import json,pathlib,sys
p=pathlib.Path.home()/'.config/lifetech-launcher/peers.json'
old=json.loads(p.read_text());v=json.load(sys.stdin)
if old['name']!=v['name'] or old['key']!=v['key']:raise RuntimeError('Fleet identity mismatch')
t=p.with_suffix('.tmp');t.touch(mode=0o600);t.chmod(0o600);t.write_text(json.dumps(old|v));t.replace(p)
'''
        try:
            subprocess.run(command + [peer_user + '@' + peer, 'python3 -c ' + shlex.quote(code)],
                           input=json.dumps({**config, 'name': peer}), text=True,
                           capture_output=True, check=True, timeout=15)
            print('Updated fleet membership on ' + peer, flush=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print('Fleet config pending on ' + peer + ': SSH unavailable; rerun onboarding when online', flush=True)


if __name__ == '__main__':
    main()
