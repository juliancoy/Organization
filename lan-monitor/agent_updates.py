"""Stage the Start request's launcher sources through existing authenticated SSH."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

FILES=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
INSTALL = '''import hashlib,json,os,pathlib,runpy,sys,tempfile
payload=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=payload['machine']:
 raise RuntimeError('SSH host differs from the authenticated agent machine')
config=pathlib.Path(os.environ.get('LIFETECH_PEER_CONFIG',str(pathlib.Path.home()/'.config/lifetech-launcher/peers.json')))
settings=json.loads(config.read_text())
if settings['name']!=payload['node']:
 raise RuntimeError('SSH account is not the enrolled launcher owner')
public_key=payload['controller_public_key']
if len(bytes.fromhex(public_key))!=32:raise RuntimeError('Invalid controller public key')
settings['controller_public_key']=public_key
pending=config.with_suffix('.controller.tmp');pending.write_text(json.dumps(settings,indent=2)+'\\n');pending.chmod(0o600);pending.replace(config)
files=payload['files']
expected=('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py')
if set(files)!=set(expected)|{'launcher_update.py'}:
 raise RuntimeError('Incomplete launcher update')
if hashlib.sha256(b''.join(files[name].encode() for name in expected)).hexdigest()!=payload['revision']:
 raise RuntimeError('Launcher update checksum mismatch')
for name,source in files.items():compile(source,name,'exec')
with tempfile.TemporaryDirectory(prefix='launcher-start-update-') as temporary:
 for name,source in files.items():(pathlib.Path(temporary)/name).write_text(source)
 sys.argv=['launcher_update.py']
 runpy.run_path(str(pathlib.Path(temporary)/'launcher_update.py'),run_name='__main__')
'''


class LauncherUpdates:
    def __init__(self,root,stopped,progress):
        self.stopped,self.progress=stopped,progress
        folder=Path(root)/'scripts'
        self.files={name:(folder/name).read_text() for name in (*FILES,'launcher_update.py')}
        self.revision=hashlib.sha256(b''.join(self.files[name].encode() for name in FILES)).hexdigest()

    def stage(self,host,status):
        if self.stopped():raise InterruptedError('Launcher update stopped')
        from agent_client import CONFIG
        public_key=json.loads(CONFIG.read_text())['controller_public_key']
        if len(bytes.fromhex(public_key))!=32:raise ValueError('Invalid pinned controller key')
        payload={'machine':status['machine'],'node':status['node'],'revision':self.revision,'files':self.files,'controller_public_key':public_key}
        if host.get('local'):
            command=[sys.executable,'-c',INSTALL]
        else:
            inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
            accounts=json.loads(inventory.read_text()).get('accounts',{}) if inventory.exists() else {}
            alias=host['host'] if host['host'].endswith('.local') else host['host']+'.local'
            command=['ssh','-o','AddressFamily=any','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                     '-o','ForwardAgent=no','-o','HostName='+host['lan_address'],'-o','HostKeyAlias='+alias]
            key=Path.home()/'.ssh/orgportal_lan_ed25519'
            if key.exists():command+=['-i',str(key)]
            user=accounts.get(alias)
            if user:command+=['-l',user]
            command += [alias,'python3 -c '+shlex.quote(INSTALL)]
        result=subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,timeout=40)
        if result.returncode:
            # No source payload, fleet keys or account configuration enter run logs.
            raise RuntimeError('Launcher staging failed over '+('local access' if host.get('local') else 'SSH')+
                               '; check saved access, host keys and enrolled account. '+result.stderr[-400:])

    def ensure(self,host,client,status):
        if status.get('source_revision')==self.revision and status.get('ready'):return status
        if status.get('active_jobs') or status.get('active_image_transfers') or status.get('active_uploads'):
            raise RuntimeError('Launcher is busy; update deferred without interrupting its work')
        for ident in status.get('preparations',[]):
            client.call('job.stop',{'id':ident},timeout=40)
        identity=status['machine']
        detail={'status':'updating','verified':False,'from_revision':status.get('source_revision'),'to_revision':self.revision}
        host['session']=detail
        self.progress(host,'updating',detail)
        self.stage(host,status)
        self.progress(host,'waiting_for_update',{**detail,'status':'waiting_for_update'})
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if self.stopped():raise InterruptedError('Launcher update stopped')
            try:value=client.call('status',timeout=5)
            except Exception:value=None  # Connection loss is expected during supervisor reload.
            if value:
                if value.get('machine')!=identity:raise RuntimeError('Machine identity changed during launcher update')
                if value.get('source_revision')==self.revision and value.get('ready'):
                    host['launcher_update']={'status':'updated','from_revision':status.get('source_revision'),'to_revision':self.revision}
                    return value
            time.sleep(.5)
        raise RuntimeError('Launcher update did not become ready; supervisor may have rolled back. No tests dispatched to this machine')
