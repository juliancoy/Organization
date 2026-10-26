"""Read-only physical interface counters via enrolled hosts and saved SSH access."""
import json
from pathlib import Path
import subprocess
import sys

SCRIPT = '''import json,time
from pathlib import Path
interfaces={}
for device in sorted(Path('/sys/class/net').iterdir()):
 if device.name=='lo' or '/virtual/' in str(device.resolve()):continue
 if len(interfaces)>=32:break
 try:
  interfaces[device.name]={'ifindex':int((device/'ifindex').read_text()),'state':(device/'operstate').read_text().strip(),
   'rx_bytes':int((device/'statistics/rx_bytes').read_text()),'tx_bytes':int((device/'statistics/tx_bytes').read_text())}
 except (OSError,ValueError):continue
print(json.dumps({'monotonic':time.monotonic(),'sampled_at':time.time(),
 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'interfaces':interfaces}))
'''


def capture(target,script=SCRIPT):
    if target.get('local'):
        command=[sys.executable,'-c',script];payload=None
    else:
        from agent_client import CONFIG
        config=json.loads(CONFIG.read_text())
        address=config['peers'][target['host']]
        inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
        accounts=json.loads(inventory.read_text()).get('accounts',{}) if inventory.exists() else {}
        alias=target['host'] if target['host'].endswith('.local') else target['host']+'.local'
        command=['ssh','-o','AddressFamily=inet','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=3',
                 '-o','ForwardAgent=no','-o','HostName='+address,'-o','HostKeyAlias='+alias]
        key=Path.home()/'.ssh/orgportal_lan_ed25519'
        if key.exists():command+=['-i',str(key)]
        if accounts.get(alias):command+=['-l',accounts[alias]]
        command += [alias,'python3 -'];payload=script
    result=subprocess.run(command,input=payload,capture_output=True,text=True,timeout=5,check=True)
    if len(result.stdout)>65536:raise ValueError('Interface response exceeds limit')
    return json.loads(result.stdout)


class InterfaceSampler:
    def __init__(self,reader=capture):
        self.reader=reader;self.previous=None

    def sample(self,target):
        try:current=self.reader(target)
        except (OSError,ValueError,subprocess.SubprocessError):return []
        previous=self.previous;self.previous=current
        elapsed=current['monotonic']-previous['monotonic'] if previous else 0
        valid=previous and previous.get('boot_id')==current.get('boot_id') and 0<elapsed<=30
        rows=[]
        for name,counters in list(current.get('interfaces',{}).items())[:32]:
            row={'name':name,'ifindex':counters.get('ifindex'),'state':counters.get('state'),
                 'status':'warming','sampled_at':current['sampled_at'],'window_seconds':elapsed if valid else None,
                 'rx_mbps':None,'tx_mbps':None,'network_mbps':None}
            before=(previous.get('interfaces') or {}).get(name) if previous else None
            if valid and before and before.get('ifindex')==counters.get('ifindex'):
                changes=[counters[key]-before[key] for key in ('rx_bytes','tx_bytes')]
                if min(changes)>=0:
                    rx,tx=[round(change*8/elapsed/1000000,4) for change in changes]
                    row.update(status='live',rx_mbps=rx,tx_mbps=tx,network_mbps=round(rx+tx,4))
            rows.append(row)
        return rows
