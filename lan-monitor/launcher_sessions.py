"""Initialize missing launcher peer sessions when a user starts distributed tests."""
import concurrent.futures
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import socket
import ipaddress
import time

IMAGE = 'organization-launcher:webrtc-1'
SESSION = 'lifetech-launcher-session'


class LauncherSessions:
    def __init__(self, root, hosts, ssh, images, stopped, progress):
        self.root, self.hosts, self.ssh, self.images = Path(root), hosts, ssh, images
        self.stopped, self.progress = stopped, progress

    def command(self, host, command, **kwargs):
        if self.stopped():raise InterruptedError('Launcher initialization stopped')
        return subprocess.run(self.ssh(host)+[command],capture_output=True,timeout=45,**kwargs)

    def accessible(self):
        def probe(host):
            try:
                result=self.command(host,"python3 -c 'import sys; print(sys.version_info.major)' && docker info --format '{{.OSType}}'",text=True)
                if result.returncode or 'linux' not in result.stdout:raise RuntimeError('Docker or Python unavailable')
                host['session']={'status':'reachable'}
                return host
            except Exception as error:
                host.update(status='unavailable',error='Launcher preflight: '+type(error).__name__)
                host['session']={'status':'unavailable','error':str(error)[:200]}
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.hosts)) as pool:
            return [host for host in pool.map(probe,self.hosts) if host]

    def initialize(self, hosts):
        # Use the existing local fleet key; never rotate an established fleet on every run.
        CONFIG=Path.home()/'.config/lifetech-launcher/peers.json'
        local=json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        authority=CONFIG.parent/'session.key'
        if not local.get('key') and not authority.exists():
            authority.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            import os
            descriptor=os.open(authority,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w') as stream:stream.write(secrets.token_hex(32))
        key=local.get('key') or authority.read_text().strip()
        peers={}
        for host in self.hosts:
            try:
                address=socket.gethostbyname(host['host'])
                if not ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback:
                    raise ValueError('Expected a private LAN address')
                peers[host['host']]=address
            except (OSError,ValueError):
                if any(active['host']==host['host'] for active in hosts) and not host.get('local'):raise RuntimeError('Cannot resolve LAN address for '+host['host'])
                peers[host['host']]=host['host']
        local_host=next((host for host in hosts if host.get('local')),None)
        remote=next((host for host in hosts if not host.get('local')),None)
        if local_host and remote:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
                route.connect((peers[remote['host']],8790))
                peers[local_host['host']]=route.getsockname()[0]

        self.images.ensure(IMAGE,hosts)
        results=[]
        for host in hosts:
            self.progress(host,'initializing',None)
            config={'name':host['host'],'peers':peers,'key':key}
            code='''import pathlib,json,sys,os,subprocess,time
os.umask(0o077)
payload=json.load(sys.stdin);home=pathlib.Path.home()
config_path=home/'.config/lifetech-launcher/peers.json'
previous=json.loads(config_path.read_text()) if config_path.exists() else {}
# A healthy existing launcher owns port 8790. Reuse it, without touching its services.
for name in ('lifetech-launcher','lifetech-launcher-session'):
 r=subprocess.run(['docker','exec',name,'python','-c',"import pathlib,time; p=pathlib.Path('/tmp/launcher-peers.json'); assert p.exists() and time.time()-p.stat().st_mtime<45"],capture_output=True)
 if r.returncode==0:
  if previous.get('key')!=payload['config']['key'] or previous.get('peers')!=payload['config']['peers']:
   raise RuntimeError('Existing launcher has different fleet enrollment; re-enroll/recreate that launcher explicitly')
  print(json.dumps({'status':'ready','container':name,'reused':True}));sys.exit(0)
config=previous|payload['config']
config_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
tmp=config_path.with_suffix('.tmp');tmp.write_text(json.dumps(config));tmp.chmod(0o600);tmp.replace(config_path)
folder=home/'.local/state/lifetech-launcher/session';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,source in payload['sources'].items():(folder/name).write_text(source)
subprocess.run(['docker','rm','-f','lifetech-launcher-session'],capture_output=True)
result=subprocess.run(['docker','run','--pull=never','-d','--init','--name','lifetech-launcher-session','--restart','unless-stopped',
 '--network','host','--cpus','0.5','--memory','512m','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges',
 '--user',str(os.getuid())+':'+str(os.getgid()),'--group-add',str(pathlib.Path('/var/run/docker.sock').stat().st_gid),
 '--log-opt','max-size=2m','--log-opt','max-file=2','--label','organization.container-class=launcher',
 '--mount','type=bind,src='+str(folder)+',dst=/session,readonly',
 '--mount','type=bind,src='+str(config_path.parent)+',dst=/run/launcher-config',
 '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
 '-e','LIFETECH_PEER_CONFIG=/run/launcher-config/peers.json','-e','PYTHONUNBUFFERED=1','--no-healthcheck',
 payload['image'],'python','/session/launcher_peers.py','--prefix','lifetech-'],capture_output=True,text=True)
if result.returncode:raise RuntimeError('Launcher session container failed to start: '+result.stderr[-600:])
for _ in range(25):
 r=subprocess.run(['docker','exec','lifetech-launcher-session','python','-c',"from pathlib import Path; assert Path('/tmp/launcher-peers.json').exists()"],capture_output=True)
 if r.returncode==0:print(json.dumps({'status':'ready','container':'lifetech-launcher-session','reused':False}));break
 time.sleep(1)
else:raise RuntimeError('Launcher session did not become ready')
'''
            sources={name:(self.root/'scripts'/name).read_text() for name in ('launcher_peers.py','launcher_registration.py')}
            payload={'config':config,'sources':sources,'image':self.images.host_images[host['host']][IMAGE]}
            command='python3 -c '+shlex.quote(code)
            try:
                result=self.command(host,command,input=json.dumps(payload).encode())
                if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-800:])
                state=json.loads(result.stdout)
                state['status']='verifying'
                # Keep the local enrollment for repeatable initialization next time.
                if host.get('local'):
                    local=json.loads(CONFIG.read_text())
                host['session']=state
                self.progress(host,'verifying',state)
                results.append(host)
            except Exception as error:
                host['session']={'status':'failed','error':str(error)[:800]}
                self.progress(host,'failed',host['session'])
        if len(results)==len(hosts):
            self.verify(results)
        return results

    def verify(self, hosts):
        deadline=time.monotonic()+75
        pending=list(hosts)
        while pending and time.monotonic()<deadline:
            for host in list(pending):
                expected=[other['host'] for other in hosts if other['host']!=host['host']]
                code="import json,pathlib,time; data=json.loads(pathlib.Path('/tmp/launcher-peers.json').read_text()); peers=data['peers']; expected="+repr(expected)+"; assert all(peers.get(peer,{}).get('connection')=='connected' and time.time()-peers[peer].get('received',0)<45 for peer in expected)"
                command=shlex.join(['docker','exec',host['session']['container'],'python','-c',code])
                try:
                    if self.command(host,command).returncode==0:
                        host['session'].update(status='ready',verified=True)
                        self.progress(host,'ready',host['session'])
                        pending.remove(host)
                except (OSError,subprocess.TimeoutExpired):pass
            if pending:
                if self.stopped():raise InterruptedError('Launcher initialization stopped')
                time.sleep(1)
        for host in pending:
            host['session'].update(status='failed',error='WebRTC status was not received from every accessible machine')
            self.progress(host,'failed',host['session'])
        if pending:raise RuntimeError('Launcher mesh did not establish all accessible peer connections')
