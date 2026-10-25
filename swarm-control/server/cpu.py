"""Live host CPU, memory, disk I/O, and network samples without installing agents."""
import concurrent.futures
import datetime
import json
import os
import subprocess
import threading
import time
from distributed import targets
from agent_client import AgentClient, CONFIG

SAMPLE = '''import json,time,os,socket,subprocess
from pathlib import Path
def read():
 result={}
 for line in Path('/proc/stat').read_text().splitlines():
  fields=line.split()
  if fields and (fields[0]=='cpu' or fields[0].startswith('cpu') and fields[0][3:].isdigit()):
   values=[int(v) for v in fields[1:9]]
   result[fields[0]]=(sum(values),values[3]+values[4])
 return result
def io():
 result={}
 for device in Path('/sys/block').iterdir():
  if device.resolve().as_posix().startswith('/sys/devices/virtual/'):continue
  try:
   v=(device/'stat').read_text().split();result[device.name]=(int(v[9]),int(v[2])+int(v[6]))
  except (OSError,ValueError,IndexError):pass
 return result
def net():
 result=0
 for device in Path('/sys/class/net').iterdir():
  if device.name=='lo' or '/virtual/' in str(device.resolve()):continue
  try:result+=sum(int((device/'statistics'/field).read_text()) for field in ['rx_bytes','tx_bytes'])
  except (OSError,ValueError):pass
 return result
a=read();ia=io();na=net();start=time.monotonic();time.sleep(1);b=read();ib=io();nb=net();elapsed=time.monotonic()-start
def utilization(key):
 if key not in a:return None
 total=b[key][0]-a[key][0];idle=b[key][1]-a[key][1]
 return round(max(0,min(100,100*(total-idle)/total)),1) if total>0 else None
cores=[{'id':int(key[3:]),'percent':utilization(key)} for key in b if key!='cpu']
memory={}
for line in Path('/proc/meminfo').read_text().splitlines():
 k,v=line.split(':',1);memory[k]=int(v.split()[0])*1024
ram_total=memory.get('MemTotal',0);ram_used=max(0,ram_total-memory.get('MemAvailable',ram_total))
disks=[max(0,min(100,(ib[k][0]-ia[k][0])/elapsed/10)) for k in ib if k in ia]
try:ips=subprocess.check_output(['hostname','-I'],text=True,timeout=1).split()
except (OSError,subprocess.SubprocessError):ips=[]
print(json.dumps({'percent':utilization('cpu'),'cpus':len(cores),'logical_cpus':cores,
 'observed_hostname':socket.gethostname(),'addresses':ips,
 'ram_percent':round(100*ram_used/ram_total,1) if ram_total else None,
 'ram_used_bytes':ram_used,'ram_total_bytes':ram_total,
 'disk_busy_percent':round(max(disks),1) if disks else None,
 'disk_basis':'Busiest physical block device; Linux I/O busy time',
 'network_mbps':round(max(0,nb-na)*8/elapsed/1000000,4)}))
'''



def aggregate(hosts):
    fresh=[h for h in hosts if h.get('percent') is not None and h.get('status')=='live']
    capacity=sum(h['cpus'] for h in fresh)
    return round(sum(h['percent']*h['cpus'] for h in fresh)/capacity,1) if capacity else None


class CpuMonitor:
    def __init__(self):
        self.lock=threading.Lock()
        self.hosts=[]

    def sample(self, target):
        local=target.get('local',False)
        try:
            if local:
                result=subprocess.run(['python3','-c',SAMPLE],capture_output=True,text=True,timeout=8,check=True)
                values=json.loads(result.stdout)
            else:
                config=json.loads(CONFIG.read_text())
                address=config['peers'][target['host']]
                values=AgentClient({**target,'agent_endpoint':'http://'+address+':8790'}).call('metrics',timeout=8)
            return {'host':target['host'],**values,'status':'live','sampled_at':time.time()}
        except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError):
            return {'host':target['host'],'percent':None,'cpus':None,'status':'unavailable','sampled_at':time.time()}

    def view(self):
        with self.lock:
            hosts=[dict(h) for h in self.hosts]
        for host in hosts:
            if time.time()-host['sampled_at']>15:
                host.update(status='stale',percent=None,logical_cpus=[],ram_percent=None,disk_busy_percent=None,network_mbps=None)
        return {'hosts':hosts,'total_percent':aggregate(hosts),'live_hosts':sum(h['status']=='live' for h in hosts),
                'total_hosts':len(hosts),'sample_interval_seconds':5,
                'scope':'Whole-machine CPU utilization across compai and saved SSH hosts, weighted by logical CPU count. Includes all processes, not just tests. Unavailable hosts are excluded from the measured total.'}

    def worker(self):
        while True:
            start=time.monotonic()
            try:
                inventory=targets()
            except (OSError,ValueError):
                inventory=[]
            hosts=[{'host':'compai.local','local':True}]+[h for h in inventory if h['host']!='compai.local']
            with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
                samples=list(pool.map(self.sample,hosts))
            with self.lock:
                self.hosts=samples
            time.sleep(max(0.1,5-(time.monotonic()-start)))
