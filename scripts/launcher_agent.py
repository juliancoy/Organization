"""Authenticated, bounded LAN job protocol. No shell-command endpoint."""
import base64
import collections
import contextlib
import fcntl
import hashlib
import json
import io
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import urllib.error

class ResourceLease:
    """Cross-process CPU/RAM admission; locks release on process death.

    Every run shares one node-local pool. Waiting groups with the smallest
    dominant resource share get the next fitting slot. After 30 seconds the
    oldest waiter reserves headroom, preventing large jobs from starving.
    """
    def __init__(self, group, cpus, memory_mb, container='', directory=None, limits=None, pids=512):
        if not isinstance(group,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',group):raise ValueError('Invalid resource group')
        if type(cpus) is not int or type(memory_mb) is not int or type(pids) is not int or min(cpus,memory_mb,pids)<1:raise ValueError('Invalid resources')
        if container and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}',container):raise ValueError('Invalid container')
        self.directory=Path(directory or os.getenv('SWARM_RESOURCE_DIR',str(Path.home()/'.local/state/swarm-control/resources')))
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.limits=limits
        self.key=secrets.token_hex(16)
        self.path=self.directory/(self.key+'.json')
        self.row={'id':self.key,'group':group,'cpus':cpus,'memory_mb':memory_mb,'pids':pids,'container':container,'created':time.time(),'active':False}
        with self.guard():
            self.file=self.path.open('x+');self.path.chmod(0o600)
            fcntl.flock(self.file,fcntl.LOCK_EX);self.write()

    def guard(self):
        @contextlib.contextmanager
        def locked():
            with (self.directory/'pool.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        return locked()

    def write(self):
        self.file.seek(0);self.file.truncate();json.dump(self.row,self.file);self.file.flush()

    def capacity(self):
        if self.limits is not None:return self.limits
        memory={line.split(':',1)[0]:int(line.split()[1])//1024 for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2 and line.split()[1].isdigit()}
        limits={'cpus':max(1,len(os.sched_getaffinity(0))), 'memory_mb':max(0,memory['MemTotal']),
                'available_mb':max(0,memory['MemAvailable'])}
        # Respect every finite cgroup ancestor, including the user/service slice
        # shared by rootless Docker. Reserve room for Docker and agent threads.
        root=Path('/sys/fs/cgroup')
        group=next((line.split(':',2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')), '/')
        current=root/group.lstrip('/')
        bounds=[]
        memory_bounds=[]
        ancestors=[current,*current.parents]
        container_scope=next((parent for parent in ancestors if re.fullmatch(r'(?:docker-|libpod-)?[a-f0-9]{64}(?:\.scope)?',parent.name)),None)
        # Docker batches are sibling containers, not descendants of this agent.
        # A private cgroup namespace may hide shared ancestors completely.
        private_namespace=container_scope is None and Path('/.dockerenv').exists()
        limits['memory_scope']='host available RAM and visible shared worker cgroup ancestors'
        if private_namespace:limits['memory_scope']='host available RAM; agent-private cgroup excluded; shared cgroup ancestors not visible'
        for parent in [current,*current.parents]:
            if parent!=root and root not in parent.parents:continue
            shared_memory_scope=not private_namespace and (container_scope is None or parent in container_scope.parents)
            if shared_memory_scope:
                try:
                    maximum=(parent/'memory.max').read_text().strip()
                    if maximum!='max':memory_bounds.append((int(maximum)//1024**2,int((parent/'memory.current').read_text())//1024**2))
                except FileNotFoundError:pass
            try:
                maximum=(parent/'pids.max').read_text().strip()
                if maximum!='max':bounds.append((int(maximum),int((parent/'pids.current').read_text())))
            except FileNotFoundError:continue
        if memory_bounds:
            limits['memory_mb']=min(limits['memory_mb'],*(maximum for maximum,_ in memory_bounds))
            limits['available_mb']=max(0,min(limits['available_mb'],*(maximum-used for maximum,used in memory_bounds)))
        if bounds:
            limits['pids']=max(0,min(maximum-min(256,max(32,maximum//4)) for maximum,_ in bounds))
            limits['available_pids']=max(0,min(maximum-used-min(256,max(32,maximum//4)) for maximum,used in bounds))
        return limits

    @classmethod
    def pool_capacity(cls, directory=None, group=None):
        """Read admission headroom under the pool lock, without making a reservation."""
        probe=cls.__new__(cls)
        probe.limits=None
        probe.directory=Path(directory or os.getenv('SWARM_RESOURCE_DIR',str(Path.home()/'.local/state/swarm-control/resources')))
        probe.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        probe.path=probe.directory/'.capacity'
        with probe.guard():
            limits=probe.capacity();rows=probe.rows();active=[row for row in rows if row['active']]
            reserved=sum(row['memory_mb'] for row in active)
            # MemAvailable already excludes background pressure. Conservatively
            # subtract every outstanding reservation as well; this double-counts
            # RAM already consumed by active workers but never pledges it twice.
            available=max(0,min(limits['memory_mb'],limits.get('available_mb',limits['memory_mb']))-reserved)
            return {**limits,'available_mb':available,'reserved_mb':reserved,
                    'available_cpus':max(0,limits['cpus']-sum(row['cpus'] for row in active)),
                    'active_jobs':len(active),'group_active_jobs':sum(row['group']==group for row in active),
                    'waiting_jobs':sum(not row['active'] for row in rows)}

    def rows(self):
        rows=[]
        for path in self.directory.glob('*.json'):
            if path==self.path:rows.append(dict(self.row));continue
            try:
                with path.open('r+') as file:
                    row=json.load(file)
                    try:fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:rows.append(row);continue
                    # Keep an orphan's reservation until its container is gone.
                    if row.get('active') and row.get('container'):
                        check=subprocess.run(['docker','inspect','-f','{{.State.Running}}',row['container']],capture_output=True,text=True,timeout=5)
                        if check.returncode==0 and check.stdout.strip()=='true':rows.append(row);continue
                    path.unlink(missing_ok=True)
            except FileNotFoundError:continue
        return rows

    def try_acquire(self):
        with self.guard():
            if self.row['active']:return True
            limits=self.capacity();rows=self.rows();active=[r for r in rows if r['active']]
            keys=('cpus','memory_mb')+(('pids',) if 'pids' in limits else ())
            free={k:limits[k]-sum(r.get(k,512) for r in active) for k in keys}
            free['memory_mb']=max(0,min(limits['memory_mb'],limits.get('available_mb',limits['memory_mb']))-sum(r['memory_mb'] for r in active))
            if 'pids' in free:free['pids']=min(free['pids'],limits.get('available_pids',limits['pids']))
            if any(self.row[k]>limits[k] for k in keys):raise ValueError('Task exceeds node resource capacity')
            waiting=sorted((r for r in rows if not r['active']),key=lambda r:(r['created'],r['id']))
            oldest=waiting[0]
            def fits(row):return all(row[k]<=free[k] for k in free)
            if time.time()-oldest['created']>=30:
                chosen=oldest if fits(oldest) else None
            else:
                def share(row):
                    used={k:sum(r.get(k,512) for r in active if r['group']==row['group']) for k in free}
                    return max(used[k]/max(1,limits[k]) for k in free),row['created'],row['id']
                chosen=next(iter(sorted((r for r in waiting if fits(r)),key=share)),None)
            if not chosen or chosen['id']!=self.key:return False
            self.row['active']=True;self.write();return True

    def close(self):
        if self.file.closed:return
        with self.guard():self.path.unlink(missing_ok=True);self.file.close()


VERSION = 'lifetech-agent.v1'
LIMIT = 1024 * 1024
UNPRIVILEGED_OPS={'status','metrics','metrics.publish','metrics.fleet','worker.policy','image.inspect','registry.read','tests.request','tests.status'}


def hardware_sample(root=Path('/sys/bus/pci/devices'), disk=Path.home()):
    """Read hardware without treating GPU detection as container validation."""
    usage=shutil.disk_usage(disk)
    result={'storage':{'path':str(disk),'total_bytes':usage.total,'free_bytes':usage.free},'gpus':[]}
    vendors={'0x10de':'NVIDIA','0x8086':'Intel','0x1002':'AMD'}
    for path in sorted(root.glob('*')):
        try:
            if not (path/'class').read_text().strip().startswith('0x03'):continue
            vendor=vendors.get((path/'vendor').read_text().strip(),'Other')
            result['gpus'].append({'pci_id':path.name,'vendor':vendor,
                'name':vendor+' GPU '+(path/'device').read_text().strip(),
                'backend':'CUDA candidate' if vendor=='NVIDIA' else 'Graphics / backend unverified'})
        except OSError:continue
    if shutil.which('nvidia-smi'):
        import csv
        try:
            output=subprocess.check_output(['nvidia-smi','--query-gpu=pci.bus_id,name,memory.total,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],text=True,stderr=subprocess.DEVNULL,timeout=2)
            rows=[]
            for fields in csv.reader(output.splitlines()):
                ident,name,total,free,util,temp=[v.strip() for v in fields]
                def number(value):
                    try:return float(value)
                    except ValueError:return None
                rows.append({'pci_id':ident,'vendor':'NVIDIA','name':name,'backend':'CUDA candidate',
                             'memory_total_mib':number(total),'memory_free_mib':number(free),
                             'utilization_percent':number(util),'temperature_c':number(temp)})
            result['gpus']=[g for g in result['gpus'] if g['vendor']!='NVIDIA']+rows
        except (OSError,ValueError,subprocess.SubprocessError):pass
    return result


GPU_SMOKE = r'''import ctypes as c,json
lib=c.CDLL('libcuda.so.1')
def api(name,types):
 f=getattr(lib,name);f.argtypes=types;f.restype=c.c_int
 def call(*args):
  code=f(*args)
  if code:raise RuntimeError(name+' returned CUDA error '+str(code))
 return call
P=c.c_void_p;I=c.c_int;U=c.c_uint;S=c.c_size_t;D=c.c_uint64
api('cuInit',[U])(0);count=I();api('cuDeviceGetCount',[c.POINTER(I)])(c.byref(count))
if count.value<1:raise RuntimeError('No CUDA devices exposed to the container')
ptx=b'.version 6.0\n.target sm_52\n.address_size 64\n.visible .entry check_value(.param .u64 out){.reg .u64 %rd; .reg .u32 %r; ld.param.u64 %rd,[out]; mov.u32 %r,42; st.global.u32 [%rd],%r; ret;}\n'
rows=[]
for device in range(count.value):
 name=c.create_string_buffer(256);total=S();ctx=P();module=P();func=P();address=D()
 api('cuDeviceGetName',[P,I,I])(name,256,device)
 api('cuDeviceTotalMem_v2',[c.POINTER(S),I])(c.byref(total),device)
 api('cuCtxCreate_v2',[c.POINTER(P),U,I])(c.byref(ctx),0,device)
 try:
  api('cuModuleLoadData',[c.POINTER(P),P])(c.byref(module),c.cast(c.c_char_p(ptx),P))
  api('cuModuleGetFunction',[c.POINTER(P),P,c.c_char_p])(c.byref(func),module,b'check_value')
  api('cuMemAlloc_v2',[c.POINTER(D),S])(c.byref(address),4)
  params=(P*1)(c.cast(c.byref(address),P))
  api('cuLaunchKernel',[P,U,U,U,U,U,U,U,P,P,P])(func,1,1,1,1,1,1,0,None,params,None)
  api('cuCtxSynchronize',[])()
  output=I();api('cuMemcpyDtoH_v2',[P,D,S])(c.byref(output),address,4)
  if output.value!=42:raise RuntimeError('GPU kernel readback mismatch')
  rows.append({'name':name.value.decode(),'memory_total_mib':round(total.value/2**20),'kernel_result':output.value})
 finally:api('cuCtxDestroy_v2',[P])(ctx)
print(json.dumps({'devices':rows,'status':'passed'}))
'''

# Workspaces are disposable; coordinator results and small job receipts are separate.
WORKSPACE_BUDGET=5*1024**3
MIN_FREE_BYTES=10*1024**3

def tree_bytes(root):
    total=0;seen=set()
    for folder,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not (Path(folder)/name).is_symlink()]
        for name in files:
            path=Path(folder)/name
            try:
                stat=path.lstat();identity=(stat.st_dev,stat.st_ino)
                if not path.is_symlink() and identity not in seen:total+=stat.st_blocks*512;seen.add(identity)
            except FileNotFoundError:pass
    return total

def prune_workspaces(root,active=(),mounts=(),*,now=None,budget=WORKSPACE_BUDGET,min_free=MIN_FREE_BYTES):
    root=Path(root);now=time.time() if now is None else now
    active=set(active);records=[];removed=[]
    for state_path in root.glob('*/state.json'):
        folder=state_path.parent
        if folder.is_symlink() or not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}',folder.name):continue
        try:
            if state_path.is_symlink() or state_path.stat().st_size>1024*1024:continue
            state=json.loads(state_path.read_text())
        except (OSError,ValueError):continue
        if folder.name in active or state.get('status') not in ('completed','failed','stopped','interrupted','consumed'):continue
        if any(str(mount)==str(folder) or str(mount).startswith(str(folder)+'/') for mount in mounts):continue
        source=folder/'source'
        if source.is_symlink() or (folder/'results').is_symlink():continue
        finished=state.setdefault('finished_at',state_path.stat().st_mtime)
        size=state.get('workspace_bytes')
        if size is None:size=tree_bytes(source) if source.exists() else 0;state['workspace_bytes']=size
        records.append((finished,folder,state,size))
    records.sort(key=lambda row:row[0],reverse=True)
    failed=[folder.name for finished,folder,state,size in records if source_exists(folder) and state['status'] in ('failed','interrupted') and now-finished<86400][:2]
    prepared=[folder.name for finished,folder,state,size in records if source_exists(folder) and state.get('prepare_only') and state['status']=='completed' and now-finished<1800][:1]
    keep=set(failed+prepared);retained=sum(size for _,folder,_,size in records if folder.name in keep)
    retained+=sum(tree_bytes(folder/'results') for _,folder,_,_ in records)
    for finished,folder,state,size in reversed(records):
        source=folder/'source'
        pressure=shutil.disk_usage(root).free<min_free
        if source.exists() and (folder.name not in keep or retained>budget or pressure):
            # Keep bounded diagnostics independently of the installed dependencies.
            results=folder/'results';before=tree_bytes(results);results.mkdir(exist_ok=True,mode=0o700)
            for relative in ('visual-artifacts','swarm-web-artifacts','.local/test-status','OrgPortal/.local/test-status'):
                origin=source/relative
                if origin.is_dir() and not origin.is_symlink() and origin.resolve().is_relative_to(source.resolve()) and tree_bytes(origin)<=64*1024**2:
                    def ignore_links(directory,names):return [name for name in names if (Path(directory)/name).is_symlink()]
                    shutil.copytree(origin,results/relative.replace('/','-'),dirs_exist_ok=True,ignore=ignore_links)
            retained+=tree_bytes(results)-before
            shutil.rmtree(source)
            state.update(workspace_retained=False,workspace_bytes=0)
            removed.append(folder.name)
            if folder.name in keep:retained-=size
        temporary=folder/'state.json.tmp';temporary.write_text(json.dumps(state));temporary.replace(folder/'state.json')
        if not source.exists() and now-finished>30*86400:shutil.rmtree(folder)
    for _,folder,state,_ in reversed(records):
        results=folder/'results'
        if retained>budget and results.exists():
            size=tree_bytes(results);shutil.rmtree(results);retained-=size
    return {'removed':removed,'retained_bytes':retained,'workspace_budget_bytes':budget,
            'free_bytes':shutil.disk_usage(root).free,'minimum_free_bytes':min_free,'checked_at':now}

def prune_package_cache(root,mounts=(),budget=5*1024**3):
    root=Path(root)
    if not root.is_dir() or root.is_symlink():return 0
    entries=[]
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():continue
        stamp=path/'.last-used';used=stamp.stat().st_mtime if stamp.exists() else path.stat().st_mtime
        entries.append((used,path,tree_bytes(path)))
    total=sum(size for _,_,size in entries)
    for used,path,size in sorted(entries):
        if total<=budget:break
        if any(str(mount)==str(path) or str(mount).startswith(str(path)+'/') for mount in mounts):continue
        with (root/(path.name+'.lock')).open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            shutil.rmtree(path);total-=size
    return total

def source_exists(folder):return (folder/'source').is_dir()


def verify_controller(value, config):
    if value.get('op') in UNPRIVILEGED_OPS:return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if value.get('from')!=config.get('master','compai.local'):raise ValueError('Execution requires the enrolled controller')
    signed={k:v for k,v in value.items() if k!='controller_signature'}
    try:
        key=Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['controller_public_key']))
        key.verify(base64.b64decode(value['controller_signature'],validate=True),json.dumps(signed,sort_keys=True,separators=(',',':')).encode())
    except Exception:raise ValueError('Controller signature rejected') from None




def browser_request(container,path,data,method):
    # Execute inside the owned browser namespace: rootless host networking is
    # separate from the physical host's loopback namespace.
    script="""import json,sys,urllib.request,urllib.error
from pathlib import Path
source=Path(sys.argv[1])
try:value=json.loads(source.read_text())
finally:source.unlink()
body=json.dumps(value['data']).encode() if value['data'] is not None else None
request=urllib.request.Request('http://127.0.0.1:4449'+value['path'],data=body,method=value['method'],headers={'Content-Type':'application/json'})
try:
 response=urllib.request.urlopen(request,timeout=150)
except urllib.error.HTTPError as error:
 response=error
with response:print(response.read(4*1024*1024+1).decode())
"""
    # WebDriver scripts can exceed Linux's per-argument size limit. Keep the
    # bounded request in a private, short-lived container file instead of argv.
    name='webdriver-'+secrets.token_hex(16)+'.json'
    payload=json.dumps({'path':path,'data':data,'method':method}).encode()
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as stream:
        info=tarfile.TarInfo(name);info.size=len(payload);info.mode=0o600;info.uid=1200;info.gid=1201
        stream.addfile(info,io.BytesIO(payload))
    if not container.put_archive('/tmp',archive.getvalue()):raise RuntimeError('Owned browser request staging failed')
    code,output=container.exec_run(['/usr/bin/python3','-c',script,'/tmp/'+name],user='1200:1201')
    if code or len(output)>4*1024*1024:raise RuntimeError('Owned browser WebDriver request failed')
    return json.loads(output)

def worker_policy(config, power_root=Path('/sys/class/power_supply')):
    """Local owner policy; unknown laptop power fails closed in automatic mode."""
    path=Path(config).parent/'worker-policy.json'
    try:
        mode=json.loads(path.read_text()).get('mode','auto') if path.exists() else 'auto'
        if mode not in ('auto','always','paused'):raise ValueError('Invalid worker mode')
    except (OSError,ValueError,TypeError):
        return {'mode':'invalid','accepting_jobs':False,'reason':'Worker policy is unreadable'}
    batteries=[];online=[]
    try:
        for device in power_root.iterdir():
            kind=(device/'type').read_text().strip()
            if kind=='Battery':
                if (device/'scope').exists() and (device/'scope').read_text().strip()=='Device':continue
                batteries.append({'status':(device/'status').read_text().strip(),
                                  'percent':int((device/'capacity').read_text())})
            elif (device/'online').exists():online.append((device/'online').read_text().strip()=='1')
    except (OSError,ValueError):
        return {'mode':mode,'accepting_jobs':mode=='always','reason':'Power state unavailable','batteries':batteries}
    on_battery=bool(batteries) and (not any(online) if online else any(b['status']!='Charging' for b in batteries))
    accepting=mode=='always' or (mode=='auto' and not on_battery)
    return {'mode':mode,'accepting_jobs':accepting,'on_battery':on_battery,'batteries':batteries,
            'reason':'Submit only; new worker jobs paused' if mode=='paused' else
                     'Saving battery; new worker jobs paused' if not accepting else 'Accepting worker jobs'}


def worker_environment(home):
    # The Docker CLI must use the same local engine as this agent, including
    # rootless sockets. Deployment and login credentials stay out of jobs.
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'DOCKER_HOST') if key in os.environ}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    return env


METRIC_SAMPLE = "import json,time,socket,subprocess\nfrom pathlib import Path\nresult={'monotonic':time.monotonic(),'sampled_at':time.time(),'cpu_counters':None,'cpu_error':None,\n        'ram_percent':None,'ram_used_bytes':None,'ram_total_bytes':None,'disk_counters':{},'network_bytes':None,\n        'disk_basis':'Busiest physical block device; Linux I/O busy time','cpu_pressure_some_percent':None}\ntry:\n result['boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()\nexcept OSError:result['boot_id']=None\ntry:\n result['cpu_counters']={v[0]:[int(x) for x in v[1:9]] for line in Path('/proc/stat').read_text().splitlines() if (v:=line.split()) and (v[0]=='cpu' or v[0].startswith('cpu') and v[0][3:].isdigit())}\nexcept PermissionError:result['cpu_error']='permission_denied'\nexcept (OSError,ValueError):result['cpu_error']='unavailable'\ntry:\n memory={k:int(v.split()[0])*1024 for line in Path('/proc/meminfo').read_text().splitlines() for k,v in [line.split(':',1)]}\n total=memory['MemTotal'];used=total-memory['MemAvailable']\n result.update(ram_total_bytes=total,ram_used_bytes=used,ram_percent=round(100*used/total,1))\nexcept (OSError,ValueError,KeyError):pass\ntry:\n for device in Path('/sys/block').iterdir():\n  if '/virtual/' in str(device.resolve()):continue\n  try:result['disk_counters'][device.name]=int((device/'stat').read_text().split()[9])\n  except (OSError,ValueError,IndexError):pass\nexcept OSError:pass\ntry:\n counters=[]\n for device in Path('/sys/class/net').iterdir():\n  if device.name=='lo' or '/virtual/' in str(device.resolve()):continue\n  counters.append(sum(int((device/'statistics'/field).read_text()) for field in ('rx_bytes','tx_bytes')))\n result['network_bytes']=sum(counters) if counters else None\nexcept (OSError,ValueError):pass\ntry:\n pressure=next(line.split() for line in Path('/proc/pressure/cpu').read_text().splitlines() if line.startswith('some '))\n result['cpu_pressure_some_percent']=float(dict(v.split('=') for v in pressure[1:])['avg10'])\nexcept (OSError,ValueError,KeyError,StopIteration):pass\ntry:result['addresses']=subprocess.check_output(['hostname','-I'],text=True,timeout=1).split()\nexcept (OSError,subprocess.SubprocessError):result['addresses']=[]\nresult['observed_hostname']=socket.gethostname()\nprint(json.dumps(result))\n"

class ResourceSampler:
    """Counter deltas span the entire interval between snapshots, including RPC gaps."""
    def __init__(self, reader=None):
        self.reader=reader or self.capture
        self.previous=None
        self.lock=threading.Lock()

    @staticmethod
    def capture():
        result=subprocess.run([sys.executable,'-c',METRIC_SAMPLE],capture_output=True,text=True,timeout=5,check=True)
        return json.loads(result.stdout)

    def sample(self):
        with self.lock:
            current=self.reader();previous=self.previous;self.previous=current
            counters=current['cpu_counters']
            result={k:v for k,v in current.items() if k not in ('cpu_counters','disk_counters','network_bytes','monotonic','boot_id')}
            result.update(measurement_version=2,percent=None,logical_cpus=[],window_seconds=None,
                          cpu_iowait_percent=None,cpu_steal_percent=None,network_mbps=None,disk_busy_percent=None,
                          cpu_busy_logical_cpus=None)
            result['cpus']=len([k for k in counters or {} if k!='cpu']) or None
            if current.get('cpu_error'):
                result['status']=current['cpu_error'];return result
            elapsed=current['monotonic']-previous['monotonic'] if previous else 0
            if not previous or previous.get('boot_id')!=current.get('boot_id') or not 0<elapsed<=30:
                result['status']='warming';return result
            def delta(key):
                before=(previous.get('cpu_counters') or {}).get(key);after=(counters or {}).get(key)
                if before is None or after is None or len(before)!=len(after):return None
                values=[b-a for a,b in zip(before,after)]
                return values if all(v>=0 for v in values) and sum(values)>0 else None
            def percent(values,index=None):
                if values is None:return None
                value=values[index] if index is not None else sum(values)-values[3]-values[4]-values[7]
                return round(100*value/sum(values),1)
            aggregate=delta('cpu')
            if aggregate is None or set(previous.get('cpu_counters') or {})!=set(counters or {}):
                result['status']='warming';return result
            result.update(status='live',percent=percent(aggregate),window_seconds=round(elapsed,3),
                          cpu_iowait_percent=percent(aggregate,4),cpu_steal_percent=percent(aggregate,7),
                          logical_cpus=[{'id':int(k[3:]),'percent':percent(delta(k))} for k in counters if k!='cpu'])
            result['cpu_busy_logical_cpus']=round(result['percent']*result['cpus']/100,2)
            disks=[100*(value-previous.get('disk_counters',{}).get(k,value))/(elapsed*1000)
                   for k,value in current.get('disk_counters',{}).items()
                   if k in previous.get('disk_counters',{}) and value>=previous['disk_counters'][k]]
            result['disk_busy_percent']=round(min(100,max(disks)),1) if disks else None
            if current.get('network_bytes') is not None and previous.get('network_bytes') is not None:
                change=current['network_bytes']-previous['network_bytes']
                if change>=0:result['network_mbps']=round(change*8/elapsed/1000000,4)
            return result


# Local telemetry collectors live in the atomic launcher release. They never SSH.
import copy
import math
import tempfile
PROFILE_LOCK=threading.Lock()


def telemetry_capture(script):
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=4,check=True)
    if len(result.stdout)>65536:raise ValueError('Telemetry response exceeds limit')
    return json.loads(result.stdout)


INTERFACE_SCRIPT = r'''import json,time,re,subprocess
from pathlib import Path
interfaces={}
for device in sorted(Path('/sys/class/net').iterdir()):
 if device.name=='lo' or '/virtual/' in str(device.resolve()):continue
 if len(interfaces)>=32:break
 try:
  interfaces[device.name]={'ifindex':int((device/'ifindex').read_text()),'state':(device/'operstate').read_text().strip(),
   'rx_bytes':int((device/'statistics/rx_bytes').read_text()),'tx_bytes':int((device/'statistics/tx_bytes').read_text())}
 except (OSError,ValueError):continue
 row=interfaces[device.name]
 try:
  speed=int((device/'speed').read_text())
  if speed>0:row['link_speed_mbps']=speed
 except (OSError,ValueError):pass
 try:
  duplex=(device/'duplex').read_text().strip()
  if duplex in ('full','half'):row['link_duplex']=duplex
 except OSError:pass
 if (device/'wireless').exists() or (device/'phy80211').exists():
  row.pop('link_speed_mbps',None);row.pop('link_duplex',None)
  try:
   result=subprocess.run(['iw','dev',device.name,'link'],capture_output=True,text=True,timeout=.5)
   for direction,rate in re.findall(r'(rx|tx) bitrate:\s*([0-9.]+) MBit/s',result.stdout):
    value=float(rate)
    if value>0:row['link_'+direction+'_mbps']=value
  except (OSError,ValueError,subprocess.SubprocessError):pass
print(json.dumps({'monotonic':time.monotonic(),'sampled_at':time.time(),
 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'interfaces':interfaces}))
'''


class InterfaceSampler:
    def __init__(self,reader=None):
        self.reader=reader or (lambda target: telemetry_capture(INTERFACE_SCRIPT));self.previous=None;self.seen={}

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
            for field in ('link_speed_mbps','link_rx_mbps','link_tx_mbps'):
                value=counters.get(field)
                if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value<1000000:row[field]=value
            if counters.get('link_duplex') in ('full','half'):row['link_duplex']=counters['link_duplex']
            before=(previous.get('interfaces') or {}).get(name) if previous else None
            if valid and before and before.get('ifindex')==counters.get('ifindex'):
                changes=[counters[key]-before[key] for key in ('rx_bytes','tx_bytes')]
                if min(changes)>=0:
                    rx,tx=[round(change*8/elapsed/1000000,4) for change in changes]
                    row.update(status='live',rx_mbps=rx,tx_mbps=tx,network_mbps=round(rx+tx,4))
            if row['state'] in ('down','lowerlayerdown','notpresent'):
                row.update(status='disconnected',rx_mbps=None,tx_mbps=None,network_mbps=None)
            rows.append(row)
            self.seen[name]=dict(row)
        present={row['name'] for row in rows}
        for name,row in self.seen.items():
            if name not in present and len(rows)<32:
                rows.append({**row,'state':'notpresent','status':'disconnected','sampled_at':current['sampled_at'],
                             'rx_mbps':None,'tx_mbps':None,'network_mbps':None})
        self.seen={row['name']:dict(row) for row in rows}
        return rows


DISK_SCRIPT=r'''import json,time,hashlib
from pathlib import Path
rows={}
for device in Path('/sys/block').iterdir():
 if '/virtual/' in str(device.resolve()):continue
 try:
  stat=[int(value) for value in (device/'stat').read_text().split()]
  size=int((device/'size').read_text())*512
  if size<=0:continue
  try:
   if (device/'device/type').read_text().strip()=='5':continue
  except OSError:pass
  identity=''
  for key in ('device/wwid','device/serial','wwid'):
   try:identity=(device/key).read_text().strip();break
   except OSError:pass
  model=''
  try:model=(device/'device/model').read_text().strip()
  except OSError:pass
  rows[device.name]={'identity':hashlib.sha256((device.name+':'+str(size)+':'+identity+':'+model).encode()).hexdigest(),'model':model,'read_bytes':stat[2]*512,'write_bytes':stat[6]*512,'busy_ms':stat[9]}
 except (OSError,ValueError,IndexError):continue
print(json.dumps({'monotonic':time.monotonic(),'sampled_at':time.time(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'disks':rows}))
'''


class DiskSampler:
    def __init__(self,reader=None,capacities=None):
        self.reader=reader or (lambda target:telemetry_capture(DISK_SCRIPT));self.previous=None;self.capacities=Path(capacities or Path.home()/'.config/lifetech-launcher/disk-capacities.json');self.windows={}

    def sample(self,target):
        try:current=self.reader(target)
        except (OSError,ValueError,TypeError,KeyError,RuntimeError,subprocess.SubprocessError):return []
        previous=self.previous;self.previous=current
        try:profiles=json.loads(self.capacities.read_text())
        except (OSError,ValueError):profiles={}
        elapsed=current['monotonic']-previous['monotonic'] if previous else 0
        valid=bool(previous and previous['boot_id']==current['boot_id'] and 0<elapsed<=30)
        rows=[]
        for name,after in list(current['disks'].items())[:32]:
            profile=profiles.get(target['host'],{}).get(after['identity'],{})
            capacity=profile.get('total_bytes_per_second')
            calibrated=isinstance(capacity,(int,float)) and not isinstance(capacity,bool) and 0<capacity<1e13 and profile.get('busy_percent',0)>=50
            row={'name':name,'model':after.get('model',''),'identity':after['identity'],'sampled_at':current['sampled_at'],'status':'warming',
                 'read_bytes_per_second':None,'write_bytes_per_second':None,'read_percent':None,'write_percent':None,'total_percent':None,'busy_percent':None,
                 'capacity_basis':profile.get('basis') if calibrated else None}
            if calibrated:row['capacity_bytes_per_second']=capacity
            before=previous.get('disks',{}).get(name) if valid else None
            if before and before.get('identity')==after['identity']:
                changes={key:after[key]-before[key] for key in ('read_bytes','write_bytes','busy_ms')}
                if min(changes.values())>=0:
                    row.update(status='live',busy_percent=round(min(100,100*changes['busy_ms']/elapsed/1000),1))
                    for direction in ('read','write'):row[direction+'_bytes_per_second']=round(changes[direction+'_bytes']/elapsed,1)
                    frames=self.windows.setdefault(after['identity'],collections.deque())
                    frames.append((elapsed,changes['read_bytes']+changes['write_bytes'],elapsed*row['busy_percent']))
                    while len(frames)>1 and sum(frame[0] for frame in frames)-frames[0][0]>=30:frames.popleft()
                    duration=sum(frame[0] for frame in frames)
                    busy=sum(frame[2] for frame in frames)/duration
                    if duration>=30 and busy>=50:
                        observed=sum(frame[1] for frame in frames)/duration
                        if observed>0 and (not calibrated or observed>capacity):
                            capacity=observed;calibrated=True
                            row.update(capacity_basis='observed sustained',capacity_bytes_per_second=capacity)
                            self.save_profile(target['host'],after['identity'],capacity,current['sampled_at'],duration,busy)
                    if calibrated:
                        parts={direction:100*row[direction+'_bytes_per_second']/capacity for direction in ('read','write')}
                        row.update({direction+'_percent':round(value,1) for direction,value in parts.items()});row['total_percent']=round(sum(parts.values()),1)
                else:self.windows.pop(after['identity'],None)
            else:self.windows.pop(after['identity'],None)
            rows.append(row)
        return rows

    def save_profile(self,host,identity,capacity,sampled_at,window_seconds,busy_percent):
        try:
            with PROFILE_LOCK:
                try:profiles=json.loads(self.capacities.read_text())
                except (OSError,ValueError):profiles={}
                previous=profiles.setdefault(host,{}).get(identity,{})
                if previous.get('busy_percent',0)>=50 and previous.get('total_bytes_per_second',0)>=capacity:return
                profiles[host][identity]={'total_bytes_per_second':capacity,'basis':'observed sustained','sampled_at':sampled_at,'window_seconds':window_seconds,'busy_percent':busy_percent}
                self.capacities.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w',dir=self.capacities.parent,delete=False) as stream:
                    json.dump(profiles,stream);pending=Path(stream.name)
                pending.replace(self.capacities)
        except OSError:pass


GPU_SCRIPT=r'''import json,time,re
from pathlib import Path
cards={};clients={}
for card in Path('/sys/class/drm').glob('card[0-9]*'):
 if '-' in card.name:continue
 try:
  device=(card/'device').resolve();pci=device.name
  if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]',pci):continue
  row={'pci_id':pci,'vendor_id':(device/'vendor').read_text().strip()}
  for file,key in [('gpu_busy_percent','busy'),('mem_info_vram_total','memory_total'),('mem_info_vram_used','memory_used')]:
   try:row[key]=int((device/file).read_text())
   except (OSError,ValueError):pass
  cards[pci]=row
 except OSError:pass
reads=0
for proc in Path('/proc').glob('[0-9]*'):
 if reads>=4096:break
 try:
  for fd in (proc/'fd').iterdir():
   if reads>=4096:break
   try:
    if not str(fd.readlink()).startswith('/dev/dri/'):continue
    reads+=1;text=(proc/'fdinfo'/fd.name).read_text()
    values=dict(line.split(':',1) for line in text.splitlines() if ':' in line)
    pci=values.get('drm-pdev','').strip();ident=values.get('drm-client-id','').strip()
    if pci not in cards or not ident:continue
    engines={}
    for key,value in values.items():
     if key.startswith('drm-engine-') and not key.startswith('drm-engine-capacity-'):
      match=re.fullmatch(r'\s*(\d+) ns\s*',value)
      if match:engines[key[11:]]=int(match.group(1))
    clients[pci+':'+ident]={'pci_id':pci,'engines':engines,'capacity':{k[20:]:int(v) for k,v in values.items() if k.startswith('drm-engine-capacity-') and v.strip().isdigit()}}
   except (OSError,ValueError):pass
 except OSError:pass
print(json.dumps({'monotonic':time.monotonic(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'cards':cards,'clients':clients}))
'''


def activity(previous,current):
    elapsed=current['monotonic']-previous['monotonic'] if previous else 0
    valid=previous and current.get('boot_id')==previous.get('boot_id') and 0<elapsed<=30
    results={}
    for pci,card in current.get('cards',{}).items():
        row={}
        if card.get('busy') is not None:
            row.update(utilization_percent=max(0,min(100,card['busy'])),utilization_basis='Driver GPU busy counter')
        elif valid:
            engines={};capacities={}
            for ident,client in current.get('clients',{}).items():
                before=previous.get('clients',{}).get(ident)
                if client['pci_id']!=pci or not before:continue
                for engine,value in client['engines'].items():
                    prior=before.get('engines',{}).get(engine)
                    if prior is None or value<prior:continue
                    engines[engine]=engines.get(engine,0)+value-prior
                    capacities[engine]=max(1,capacities.get(engine,1),client.get('capacity',{}).get(engine,1))
            if engines:
                row.update(utilization_percent=round(min(100,max(value/(elapsed*1e9*capacities[engine])*100 for engine,value in engines.items())),1),utilization_basis='Busiest GPU engine across visible DRM clients; other users may be excluded')
        if card.get('memory_total',0)>0 and 0<=card.get('memory_used',-1)<=card['memory_total']:
            row.update(memory_total_mib=card['memory_total']/2**20,memory_free_mib=(card['memory_total']-card['memory_used'])/2**20)
        results[pci]=row
    return results


class GpuSampler:
    def __init__(self,reader=None):
        self.reader=reader or (lambda target:telemetry_capture(GPU_SCRIPT));self.previous=None

    def sample(self,target,hardware):
        try:
            current=self.reader(target);rows=activity(self.previous,current);self.previous=current
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return hardware
        return {**hardware,'gpus':[{**gpu,**rows.get(gpu.get('pci_id'),{})} for gpu in hardware.get('gpus',[])]}


class LauncherTelemetry:
    """Independent local collectors and bounded, acknowledged five-minute replay."""
    def __init__(self, agent):
        self.agent=agent
        self.lock=threading.RLock()
        self.stopped=threading.Event()
        self.session=secrets.token_hex(16)
        self.sequence=0
        self.revision=0
        self.samples=collections.deque(maxlen=256)
        self.received={}
        self.latest={}
        self.threads=[]

    def record(self, kind, value, sampled_at=None):
        sampled_at=time.time() if sampled_at is None else sampled_at
        with self.lock:
            self.sequence+=1
            self.samples.append({'sequence':self.sequence,'kind':kind,'sampled_at':sampled_at,'value':copy.deepcopy(value)})
            self.latest[kind]={'sampled_at':sampled_at,'value':copy.deepcopy(value)}

    def pending(self, acknowledged):
        with self.lock:
            rows=[row for row in self.samples if row['sequence'] not in acknowledged and row['sampled_at']>=time.time()-295]
            # Prioritize the newest measurement of each kind after reconnecting.
            newest={row['kind']:row for row in rows}
            selected={row['sequence']:row for row in list(newest.values())+rows[:12]}
            return {'session':self.session,'samples':copy.deepcopy(list(selected.values()))}

    def accept(self, sender, args):
        session=args.get('session');rows=args.get('samples')
        if not isinstance(session,str) or not re.fullmatch('[a-f0-9]{32}',session):raise ValueError('Invalid telemetry session')
        if not isinstance(rows,list) or not 1<=len(rows)<=16 or len(json.dumps(args,allow_nan=False))>262144:raise ValueError('Invalid telemetry batch')
        now=time.time()
        for row in rows:
            if (not isinstance(row,dict) or type(row.get('sequence')) is not int or row['sequence']<1
                    or row.get('kind') not in ('cpu','hardware','disks','network_interfaces')
                    or type(row.get('sampled_at')) not in (int,float) or not math.isfinite(row['sampled_at'])
                    or not now-300<=row['sampled_at']<=now+5):raise ValueError('Invalid telemetry sample')
            value=row.get('value')
            if row['kind'] in ('disks','network_interfaces'):
                if not isinstance(value,list) or len(value)>32 or any(not isinstance(item,dict) for item in value):raise ValueError('Invalid telemetry rows')
            elif not isinstance(value,dict):raise ValueError('Invalid telemetry value')
            elif row['kind']=='hardware' and (not isinstance(value.get('gpus',[]),list) or len(value.get('gpus',[]))>16 or any(not isinstance(gpu,dict) for gpu in value.get('gpus',[]))):raise ValueError('Invalid GPU telemetry')
            elif row['kind']=='cpu' and (value.get('status') not in ('live','warming','permission_denied','unavailable')
                    or type(value.get('sampled_at')) not in (int,float) or value['sampled_at']!=row['sampled_at']):raise ValueError('Invalid CPU telemetry')
            if row['kind']=='cpu' and value['status']=='live':
                if (type(value.get('cpus')) is not int or not 1<=value['cpus']<=65536
                        or type(value.get('percent')) not in (int,float) or not math.isfinite(value['percent'])
                        or not 0<=value['percent']<=100):raise ValueError('Invalid live CPU measurement')
        with self.lock:
            entry=self.received.setdefault(sender,{'events':collections.deque(maxlen=256),'seen':set(),'latest':{}})
            for row in rows:
                ident=(session,row['sequence'])
                if ident in entry['seen']:continue
                self.revision+=1
                event={**copy.deepcopy(row),'session':session,'revision':self.revision}
                entry['events'].append(event)
                before=entry['latest'].get(row['kind'])
                if before is None or row['sampled_at']>before['sampled_at']:entry['latest'][row['kind']]=event
                entry['seen'].add(ident)
            entry['events']=collections.deque((row for row in entry['events'] if row['sampled_at']>=now-300),maxlen=256)
            entry['seen']={(row['session'],row['sequence']) for row in entry['events']}
        return {'session':session,'sequences':[row['sequence'] for row in rows]}

    @staticmethod
    def host(name, latest):
        cpu=latest.get('cpu',{})
        value={'status':'warming',**cpu.get('value',{}),'host':name,'sampled_at':cpu.get('sampled_at',0)}
        for kind in ('hardware','disks','network_interfaces'):
            if kind in latest:
                value[kind]=copy.deepcopy(latest[kind]['value'])
                if kind=='hardware':value[kind]['sampled_at']=latest[kind]['sampled_at']
        value['telemetry_transport']='launcher-push'
        return value

    def fleet(self, enrolled, after=0):
        now=time.time()
        with self.lock:
            hosts=[];events=[]
            for name,entry in self.received.items():
                if name not in enrolled:continue
                hosts.append(self.host(name,entry['latest']))
                # Each event carries a snapshot evaluated at its own measurement time.
                latest={}
                for row in sorted(entry['events'],key=lambda row:row['sampled_at']):
                    latest[row['kind']]=row
                    if row['sampled_at']>=now-300 and row['revision']>after:
                        # Historical charts need aggregate counters, not a copy of
                        # every logical CPU in every peripheral event.
                        snapshot=self.host(name,latest)
                        snapshot.pop('logical_cpus',None)
                        if 'hardware' in snapshot:
                            snapshot['hardware']={key:value for key,value in snapshot['hardware'].items() if key in ('gpus','sampled_at','stale')}
                        events.append({'revision':row['revision'],'sampled_at':row['sampled_at'],'hosts':[snapshot]})
            events=sorted(events,key=lambda row:row['revision'])[:128]
            cursor=events[-1]['revision'] if events else self.revision
            return {'hosts':hosts,'events':events,'session':self.session,'revision':cursor}

    def start(self):
        try:config=json.loads(self.agent.config.read_text())
        except (OSError,ValueError):config={}
        target={'local':True,'host':config.get('name','local')}
        interfaces=InterfaceSampler();disks=DiskSampler();gpu=GpuSampler()
        def hardware():
            value=gpu.sample(target,hardware_sample(disk=self.agent.root))
            value['sampled_at']=time.time()
            return value
        collectors={'cpu':self.agent.metrics_sampler.sample,'hardware':hardware,
                    'network_interfaces':lambda:interfaces.sample(target),'disks':lambda:disks.sample(target)}
        def run(kind, collect):
            import logging
            while not self.stopped.is_set():
                started=time.monotonic()
                try:
                    value=collect()
                    # A failed collector does not renew the previous measurement's timestamp.
                    if value!=[]:self.record(kind,value,value.get('sampled_at') if isinstance(value,dict) else None)
                except Exception as error:logging.debug('Telemetry %s failed: %s',kind,type(error).__name__)
                self.stopped.wait(max(.1,5-(time.monotonic()-started)))
        for kind,collect in collectors.items():
            thread=threading.Thread(target=run,args=(kind,collect),name='telemetry-'+kind,daemon=True)
            thread.start();self.threads.append(thread)

    def snapshot(self, name):
        with self.lock:return self.host(name,self.latest)


class Agent:
    def __init__(self, config, root):
        self.config = config
        folder=Path(__file__).resolve().parent
        self.source_revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.nonces = {}
        self.metrics_sampler=ResourceSampler()
        self.telemetry=LauncherTelemetry(self)
        self.gpu_lock=threading.Lock()
        self.gpu_validation=None
        self.hardware_cache=None
        self.hardware_sampled=0
        self.jobs = {}
        self.active_workers=set()
        self.storage_status={}
        self.storage_lock=threading.RLock()
        self.storage_stop=threading.Event()
        self.uploads = {}
        self.registry_transfers=0
        self.image_progress={}
        self.browsers=set()
        self.browser_timers={}
        self.busy()
        for path in self.root.glob('*.upload'):path.unlink()
        for path in self.root.glob('*-p2p/browser.json'):
            self.remove_containers(path.parent.name.removesuffix('-p2p'))
            shutil.rmtree(path.parent)
        # A process restart must never pretend that a previous job is still running.
        for path in self.root.glob('*/state.json'):
            if path.parent.is_symlink() or path.is_symlink():continue
            state = json.loads(path.read_text())
            if state.get('status') in ('running', 'starting'):
                (path.parent/'STOP').touch()
                state.update(status='interrupted', error='Agent restarted during execution')
                path.write_text(json.dumps(state))
                self.remove_containers(path.parent.name)
        threading.Thread(target=self.maintain_storage,daemon=True).start()
        self.telemetry.start()

    def cleanup_workspaces(self):
        """Fail closed if container ownership cannot be inspected."""
        import docker
        try:
            with self.storage_lock:
                with contextlib.closing(docker.from_env(timeout=5)) as client:
                    mounts=[mount['Source'] for container in client.containers.list(all=True)
                            for mount in container.attrs.get('Mounts',[]) if mount.get('Type')=='bind']
                for proc in Path('/proc').glob('[0-9]*'):
                    try:mounts.append(str((proc/'cwd').resolve(strict=True)))
                    except OSError:pass
                with self.lock:
                    active=set(self.active_workers)|{key for key,value in self.jobs.items() if value['status'] in ('running','starting')}
                summary=prune_workspaces(self.root,active,mounts)
                with self.lock:
                    for ident in summary['removed']:
                        if ident in self.jobs:self.jobs[ident]['workspace_retained']=False
                    idle=not self.active_workers
                    finished=[key for key,value in self.jobs.items() if key not in self.active_workers and value['status'] not in ('running','starting')]
                    for key in finished[:-10]:
                        if not (self.folder(key)/'source').exists():del self.jobs[key]
                if idle:summary['package_cache_bytes']=prune_package_cache(self.root/'home/.cache/organization-tests/packages',mounts)
                self.storage_status=summary
                return summary
        except Exception as error:
            self.storage_status={'error':type(error).__name__+': '+str(error)[:200]}
            return self.storage_status

    def maintain_storage(self):
        while not self.storage_stop.is_set():
            self.cleanup_workspaces()
            self.storage_stop.wait(300)

    def busy(self):
        path=Path('/tmp/launcher-agent-busy.json');temporary=path.with_suffix('.tmp')
        with self.lock:
            temporary.write_text(json.dumps({'jobs':[key for key,job in self.jobs.items() if job['status']=='running'],
                                             'browsers':sorted(self.browsers),'transfers':self.registry_transfers+len(self.uploads)+len(getattr(self,'resource_leases',{})),'time':time.time()}));temporary.replace(path)

    @staticmethod
    def image_fingerprint(image):
        metadata=image.attrs
        content={key:metadata[key] for key in ('RootFS','Config','Architecture','Os','Created')}
        content['Config']={key:value for key,value in content['Config'].items() if value is not None}
        content['Config']['WorkingDir']=content['Config'].get('WorkingDir') or ''
        return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def client(self):
        import docker
        return contextlib.closing(docker.from_env(timeout=120))

    def remove_containers(self, ident):
        with self.client() as client:
            for container in client.containers.list(all=True, filters={'name':'orgportal-test-'+ident+'-'}):
                if container.name.startswith('orgportal-test-'+ident+'-'):
                    container.remove(force=True)

    async def handle(self, request):
        import asyncio
        from aiohttp import web
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            return web.Response(body=await self.process(await request.read()),content_type='application/octet-stream')
        except ValueError:
            raise web.HTTPForbidden(text='Agent authentication rejected')

    async def process(self, raw):
        import asyncio
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config = json.loads(self.config.read_text())
        cipher = AESGCM(bytes.fromhex(config['key']))
        try:
            value = json.loads(cipher.decrypt(raw[:12], raw[12:], VERSION.encode()))
            if (value.get('to') != config['name'] or value.get('from') not in config['peers'] or
                    abs(time.time()-value['time']) > 60 or not re.fullmatch('[a-f0-9]{32}',value['nonce'])):
                raise ValueError('Invalid request identity or freshness')
            verify_controller(value,config)
            with self.lock:
                self.nonces = {n:t for n,t in self.nonces.items() if t > time.time()}
                if value['nonce'] in self.nonces or len(self.nonces)>4096:
                    raise ValueError('Replayed request or request limit')
                self.nonces[value['nonce']] = time.time()+120
            try:
                if value['op']=='metrics.publish':
                    if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry receiver is not the master')
                    result=self.telemetry.accept(value['from'],value.get('args',{}))
                    result['policy']=self.telemetry_policy()
                else:
                    result = await asyncio.to_thread(self.dispatch, value['op'], value.get('args',{}))
                reply = {'ok':True, 'result':result}
            except Exception as error:
                reply = {'ok':False,'error':type(error).__name__+': '+str(error)[:800]}
            reply.update(nonce=value['nonce'], node=config['name'], time=time.time())
            nonce = secrets.token_bytes(12)
            return nonce+cipher.encrypt(nonce,json.dumps(reply).encode(),VERSION.encode())
        except Exception as error:
            import logging
            reason=str(error)
            if reason not in {'Controller signature rejected','Execution requires the enrolled controller',
                              'Invalid request identity or freshness','Replayed request or request limit'}:
                reason='Invalid encrypted request or response'
            logging.warning('Agent RPC rejected: %s; replay entries=%d',reason,len(self.nonces))
            raise ValueError('Agent authentication rejected') from None

    def folder(self, ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}(?:-p2p)?',ident):
            raise ValueError('Invalid run ID')
        return self.root/ident

    def expire_resource(self,ident,token=None):
        with self.lock:
            entry=getattr(self,'resource_leases',{}).get(ident)
            if not entry or token is not None and entry.get('token')!=token:return
            name=entry['lease'].row.get('container')
            if entry['lease'].row['active'] and name:
                # Remove the bounded, owned batch before returning its capacity.
                result=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
                if result.returncode:
                    check=subprocess.run(['docker','inspect',name],capture_output=True,timeout=5)
                    if check.returncode==0:
                        timer=threading.Timer(20,lambda expiry_token=token:self.expire_resource(ident,expiry_token));timer.daemon=True
                        entry['timer']=timer;timer.start();return
            self.dispatch('resource.release',{'id':ident})

    def telemetry_policy(self):
        try:policy=json.loads((self.config.parent/'telemetry-transport.json').read_text())
        except FileNotFoundError:return {'transport':'tcp','updated_at':0}
        if policy.get('transport') not in ('tcp','webrtc'):raise ValueError('Invalid telemetry transport policy')
        return policy

    def save_telemetry_policy(self, policy):
        if not isinstance(policy,dict) or policy.get('transport') not in ('tcp','webrtc'):raise ValueError('Invalid telemetry transport policy')
        with self.lock:
            path=self.config.parent/'telemetry-transport.json'
            temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps(policy));temporary.chmod(0o600);temporary.replace(path)
        return policy

    def dispatch(self, op, args):
        if op=='metrics.transport':
            config=json.loads(self.config.read_text())
            if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry policy requires the master')
            if not isinstance(args,dict) or set(args)!={'transport'} or args['transport'] not in ('tcp','webrtc'):raise ValueError('Choose TCP or WebRTC')
            return self.save_telemetry_policy({'transport':args['transport'],'updated_at':time.time()})
        if op=='resource.capacity':
            if set(args)-{'group'}:raise ValueError('Invalid capacity request')
            return ResourceLease.pool_capacity(directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),group=args.get('group'))
        if op in ('resource.acquire','resource.release','resource.renew'):
            with self.lock:
                leases=getattr(self,'resource_leases',None)
                if leases is None:self.resource_leases={};leases=self.resource_leases
                ident=args.get('id')
                if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('Invalid resource lease')
                if op=='resource.release':
                    previous=leases.pop(ident,None)
                    if previous:previous['timer'].cancel();previous['lease'].close()
                    self.busy()
                    return {'released':True}
                if op=='resource.renew':
                    if set(args)!={'id'}:raise ValueError('Invalid resource renewal')
                    previous=leases.get(ident)
                    if not previous or not previous['lease'].row['active']:raise ValueError('Resource lease is not active')
                    previous['timer'].cancel();token=secrets.token_hex(16)
                    timer=threading.Timer(240,lambda expiry_token=token:self.expire_resource(ident,expiry_token));timer.daemon=True
                    previous.update(timer=timer,token=token);timer.start()
                    return {'renewed':True,'expires_in_seconds':240}
                if set(args)!={'id','group','cpus','memory_mb','container'} or type(args['cpus']) is not int or args['cpus']<1 or type(args['memory_mb']) is not int or args['memory_mb']<1 or not re.fullmatch(r'genomics-[a-f0-9]{32}',args['container']):
                    raise ValueError('Expected a bounded private batch resource request')
                policy=worker_policy(self.config)
                if not policy['accepting_jobs']:raise RuntimeError(policy['reason'])
                previous=leases.get(ident)
                if not previous:
                    if len(leases)>=128:raise RuntimeError('Resource request queue full')
                    lease=ResourceLease(args.get('group'),args.get('cpus'),args.get('memory_mb'),args.get('container',''),
                                        directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),pids=32)
                    token=secrets.token_hex(16)
                    timer=threading.Timer(300,lambda expiry_token=token:self.expire_resource(ident,expiry_token));timer.daemon=True
                    leases[ident]={'lease':lease,'timer':timer,'token':token};timer.start();self.busy()
                else:
                    lease=previous['lease']
                    if any(lease.row[k]!=args[k] for k in ('group','cpus','memory_mb','container')):raise ValueError('Resource request ID already used')
                try:acquired=lease.try_acquire()
                except ValueError:
                    leases.pop(ident)['timer'].cancel();lease.close();self.busy();raise
                if acquired and not leases[ident].get('started'):
                    leases[ident]['timer'].cancel()
                    token=secrets.token_hex(16)
                    timer=threading.Timer(240,lambda expiry_token=token:self.expire_resource(ident,expiry_token));timer.daemon=True
                    leases[ident].update(timer=timer,token=token,started=True);timer.start()
                return {'acquired':acquired}
        if op=='worker.policy':return worker_policy(self.config)
        if op in ('job.start','source.stage','p2p.start','image.pull','resource.acquire') or (op=='job.feed' and args.get('tasks')):
            policy=worker_policy(self.config)
            if not policy['accepting_jobs']:raise RuntimeError(policy['reason'])
        if op in ('tests.request','tests.status'):
            config=json.loads(self.config.read_text())
            if not config.get('master_api'):raise RuntimeError('This launcher is not a test master')
            token=(self.config.parent/'master-api-token').read_text().strip()
            if op=='tests.request' and (set(args)-{'paths','suites','force'}):raise ValueError('Invalid test selection')
            request=urllib.request.Request('http://127.0.0.1:8089/api/v1/master/'+('test-requests' if op=='tests.request' else 'status'),
                data=json.dumps(args).encode() if op=='tests.request' else None,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=30) as response:
                raw=response.read(8*1024*1024+1)
                if len(raw)>8*1024*1024:raise RuntimeError('Master response too large')
                return json.loads(raw)
        if op=='registry.read':
            from launcher_registry import read
            return read(args,self.config.parent/'layer-cache')
        if op=='image.progress':
            with self.lock:return dict(self.image_progress.get(args['image'], {}))
        if op=='image.pull':
            from launcher_registry import pull
            if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
            with self.lock:self.registry_transfers+=1;self.busy()
            try:return pull(self,args)
            finally:
                with self.lock:self.registry_transfers-=1;self.busy()
        if op == 'metrics':
            return self.telemetry.snapshot(json.loads(self.config.read_text())['name'])
        if op == 'metrics.fleet':
            config=json.loads(self.config.read_text())
            if config['name']!=config.get('master','compai.local'):raise ValueError('Telemetry receiver is not the master')
            after=args.get('after',0) if args.get('session')==self.telemetry.session else 0
            if type(after) is not int or after<0:raise ValueError('Invalid telemetry cursor')
            return {**self.telemetry.fleet(config['peers'],after),'policy':self.telemetry_policy()}
        if op == 'gpu.probe':
            if args:raise ValueError('GPU probe does not accept commands or image overrides')
            if not self.gpu_lock.acquire(blocking=False):raise RuntimeError('GPU validation already running')
            lease=None
            try:
                name='orgportal-test-gpu-'+secrets.token_hex(8)
                lease=ResourceLease('gpu-probe',1,512,name,directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')),pids=64)
                if not lease.try_acquire():return {'waiting_capacity':True}
                import docker
                with self.client() as client:
                    if 'nvidia' not in client.info().get('Runtimes',{}):raise RuntimeError('NVIDIA container runtime is not configured')
                    # Existing image only: readiness checks never pull from WAN.
                    image=client.images.get('python:3.12-bookworm')
                    container=client.containers.run(image.id,['python','-c',GPU_SMOKE],detach=True,name=name,
                        network_mode='none',read_only=True,cap_drop=['ALL'],security_opt=['no-new-privileges'],
                        mem_limit='512m',pids_limit=64,nano_cpus=1000000000,
                        device_requests=[docker.types.DeviceRequest(count=-1,capabilities=[['gpu']])])
                    try:
                        status=container.wait(timeout=40)
                        logs=container.logs().decode(errors='replace')[:4000]
                        if status['StatusCode']:raise RuntimeError('CUDA kernel probe failed: '+logs[-800:])
                        result=json.loads(logs)
                    finally:container.remove(force=True)
                self.gpu_validation={**result,'verified_at':time.time()}
                return self.gpu_validation
            finally:
                if lease:lease.close()
                self.gpu_lock.release()
        if op == 'status':
            policy=worker_policy(self.config)
            config = json.loads(self.config.read_text())
            import socket
            with self.client() as client:
                info = client.info()
            machine = Path('/etc/machine-id')
            if not machine.exists():raise RuntimeError('Host machine identity is not mounted')
            rows = json.loads(subprocess.check_output(['ip','-j','addr'],text=True,timeout=5))
            addresses = [a['local'].split('%')[0] for row in rows if row['ifname'].startswith(('en','eth','wl')) for a in row.get('addr_info',[]) if a.get('scope')!='host']
            return {'protocol':VERSION,'source_revision':self.source_revision,'node':config['name'],'ready':bool(shutil.which('docker')) and not Path('/tmp/launcher-agent-draining').exists(),'time':time.time(),
                    'machine':hashlib.sha256(machine.read_bytes()).hexdigest(),'hostname':socket.gethostname(),
                    'addresses':addresses,'platform':info['OSType']+'/'+info['Architecture'],
                    'worker_policy':policy,'capabilities':['controller-signature','github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','peer-layer-cache','mptcp-transport','file-queue','snapshot-stage','event-wait','shared-resources','renewable-resources','encrypted-backup','gpu-kernel','telemetry-push'],
                    'enrolled_peers':sorted(config['peers']),'storage':self.storage_status,
                    'hardware':self.hardware(info),
                    'preparations':[key for key,j in self.jobs.items() if j['status']=='running' and j.get('prepare_only')],
                    'active_jobs':[key for key,j in self.jobs.items() if j['status']=='running' and not j.get('prepare_only')],'active_image_transfers':self.registry_transfers,'active_uploads':len(self.uploads),'active_resource_leases':len(getattr(self,'resource_leases',{}))}
        if op.startswith('backup.'):
            return self.backup(op,args)
        if op.startswith('upload.'):
            return self.upload(op,args)
        if op == 'image.inspect':
            import docker
            with self.client() as client:
                try:
                    image = client.images.get(args['image'])
                    return {'id':image.id,'os':image.attrs['Os'],'architecture':image.attrs['Architecture'],'fingerprint':self.image_fingerprint(image)}
                except docker.errors.ImageNotFound:return None
        if op == 'source.stage':
            args={**args,'manifest':{**args['manifest'],'prepare_only':True}}
            manifest=args['manifest']
            with self.lock:
                for ident,job in self.jobs.items():
                    if (job.get('prepare_only') and job.get('workspace_retained',True) and job['status'] in ('running','completed')
                            and not (self.folder(ident)/'STOP').exists()
                            and job.get('source_sha256')==manifest.get('source_sha256')
                            and job.get('images')==manifest['hosts'][args['index']].get('images')):
                        return {'id':ident,'status':job['status']}
            return self.start(args)
        if op == 'job.start':return self.start(args)
        if op == 'job.feed':
            folder=self.folder(args['id'])
            with self.lock:
                job=self.jobs.get(args['id'])
                if not job or job['status']!='running':raise ValueError('Worker session is not running')
                if (folder/'STOP').exists():raise ValueError('Worker session is stopping')
                manifest=json.loads((folder/'manifest.json').read_text())
                if not manifest.get('granular'):raise ValueError('Worker is not a task-queue session')
                tasks=args.get('tasks',[])
                if not isinstance(tasks,list) or len(tasks)>128 or any(not isinstance(t,str) or t not in manifest['task_catalog'] for t in tasks):
                    raise ValueError('Tasks must belong to the pinned run catalog')
                inbox=folder/'tasks';inbox.mkdir(exist_ok=True,mode=0o700)
                if (inbox/'CLOSED').exists() and tasks:raise ValueError('Worker queue is closed')
                for task in tasks:
                    # Identifiers are catalog hashes, never shell commands or paths.
                    if not re.fullmatch('[a-z0-9-]{1,80}',task):raise ValueError('Invalid task identifier')
                    (inbox/task).touch(exist_ok=True)
                if args.get('p2p_finished') is True:(folder/'P2P_FINISHED').touch()
                if args.get('close'):(inbox/'CLOSED').touch()
                return {'accepted':tasks,'closed':bool(args.get('close'))}
        if op == 'job.poll':
            ident=args['id'];folder=self.folder(ident)
            with self.lock:
                job=self.jobs.get(ident)
                if job is None:
                    state=json.loads((folder/'state.json').read_text())
                    return {**state,'events':[],'cursor':0,'events_lost':True}
                cursor=int(args.get('cursor',0))
                wait=min(20,max(0,float(args.get('wait_seconds',0))))
                self.changed.wait_for(lambda:job['sequence']>cursor or job['status']!='running',timeout=wait)
                events=[{'sequence':n,'event':event} for n,event in job['events'] if n>cursor][:200]
                return {'status':job['status'],'exit_code':job.get('exit_code'),'error':job.get('error'),'preparation':job.get('preparation'),
                        'events':events,'cursor':events[-1]['sequence'] if events else cursor,
                        'events_lost':bool(job['events'] and cursor<job['events'][0][0]-1)}
        if op == 'job.stop':
            folder=self.folder(args['id']);folder.mkdir(exist_ok=True,mode=0o700)
            (folder/'STOP').touch()
            with self.changed:self.changed.notify_all()
            self.remove_containers(args['id'])
            return {'acknowledged':True}
        if op == 'p2p.start':return self.p2p_start(args)
        if op == 'p2p.webdriver':
            folder=self.folder(args['id']+'-p2p')
            if not (folder/'browser.json').exists():raise ValueError('No owned P2P browser')
            path=args['path']
            if not isinstance(path,str) or not re.fullmatch(r'/(?:status|session(?:/[A-Za-z0-9_/-]+)?)',path):raise ValueError('Invalid WebDriver path')
            method=args.get('method','POST')
            if method not in ('GET','POST','DELETE'):raise ValueError('Invalid WebDriver method')
            record=json.loads((folder/'browser.json').read_text())
            with self.client() as client:
                return browser_request(client.containers.get(record['container']),path,args.get('data'),method)
        if op == 'p2p.stop':
            ident=args['id']+'-p2p';folder=self.folder(ident)
            self.remove_containers(args['id'])
            shutil.rmtree(folder,ignore_errors=True)
            with self.lock:
                timer=self.browser_timers.pop(args['id'],None)
                if timer:timer.cancel()
                self.browsers.discard(args['id']);self.busy()
                lease=getattr(self,'p2p_leases',{}).pop(args['id'],None)
                if lease:lease.close()
            return {'acknowledged':True}
        raise ValueError('Unsupported agent operation')

    def backup(self,op,args):
        """Owner-authenticated encrypted replicas; fixed private storage, no extraction."""
        root=Path(self.config).parent/'backups';root.mkdir(exist_ok=True,mode=0o700)
        def checked(value):
            if not isinstance(value,dict) or value.get('version')!=1 or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z',str(value.get('snapshot',''))):raise ValueError('Invalid backup identity')
            if value.get('encryption')!='AES-256-GCM/RSA-OAEP-SHA256' or not all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) for k in ('sourceManifestSha256','recipientSha256')):raise ValueError('Backup must be encrypted')
            if not 256<=len(base64.b64decode(value.get('wrappedKey',''),validate=True))<=1024:raise ValueError('Invalid recipient key')
            parts=value.get('parts')
            if not isinstance(parts,list) or not 1<=len(parts)<=8192:raise ValueError('Invalid backup parts')
            if any(not isinstance(p,dict) or not re.fullmatch('[a-f0-9]{64}',str(p.get('sha256',''))) or type(p.get('size')) is not int or not 28<p['size']<=64*1024**2 for p in parts):raise ValueError('Invalid backup part')
            if len({p['sha256'] for p in parts})!=len(parts):raise ValueError('Duplicate backup parts')
            return value
        def save(path,value):
            temporary=path.with_suffix('.tmp')
            with open(temporary,'w',opener=lambda p,flags:os.open(p,flags,0o600)) as stream:
                json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        def good(path,part):
            if not path.is_file() or path.stat().st_size!=part['size']:return False
            with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()==part['sha256']
        with self.lock:
            if op=='backup.status':
                copies=[]
                for path in sorted(root.glob('*/manifest.json'),reverse=True)[:50]:
                    value=json.loads(path.read_text());copies.append({'snapshot':value['snapshot'],'parts':len(value['parts']),'bytes':sum(p['size'] for p in value['parts']),'path':str(path.parent)})
                return {'root':str(root),'freeBytes':shutil.disk_usage(root).free,'copies':copies}
            if op=='backup.begin':
                value=checked(args['manifest']);folder=root/value['snapshot'];folder.mkdir(exist_ok=True,mode=0o700)
                for name in ('manifest.json','pending.json'):
                    existing=folder/name
                    if existing.exists() and json.loads(existing.read_text())!=value:raise ValueError('Backup generation is immutable')
                missing=[p for p in value['parts'] if not good(folder/(p['sha256']+'.enc'),p)]
                if shutil.disk_usage(root).free<sum(p['size'] for p in missing)+512*1024**2:raise RuntimeError('Insufficient backup disk space')
                save(folder/'pending.json',value)
                return {'missing':[p['sha256'] for p in missing],'path':str(folder),'freeBytes':shutil.disk_usage(root).free}
            ident=args.get('snapshot','')
            if not isinstance(ident,str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z',ident):raise ValueError('Invalid backup snapshot')
            folder=root/ident
            pending=folder/'pending.json';value=checked(json.loads((pending if pending.exists() else folder/'manifest.json').read_text()))
            if op=='backup.put':
                digest=args.get('sha256');part=next((p for p in value['parts'] if p['sha256']==digest),None)
                if not part:raise ValueError('Part is not in this backup')
                source=self.completed_upload(args['upload'])
                if not good(source,part):raise ValueError('Backup upload checksum mismatch')
                target=folder/(digest+'.enc')
                if not good(target,part):
                    temporary=folder/(digest+'.pending');shutil.copyfile(source,temporary);temporary.chmod(0o600)
                    with temporary.open('rb') as stream:os.fsync(stream.fileno())
                    temporary.replace(target)
                self.release_upload(args['upload'])
                return {'stored':True,'sha256':digest,'bytes':part['size']}
            if op=='backup.commit':
                if any(not good(folder/(p['sha256']+'.enc'),p) for p in value['parts']):raise RuntimeError('Backup is incomplete or corrupt')
                save(folder/'manifest.json',value);pending.unlink(missing_ok=True)
                descriptor=os.open(folder,os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
                return {'verified':True,'snapshot':ident,'parts':len(value['parts']),'bytes':sum(p['size'] for p in value['parts']),'path':str(folder)}
            raise ValueError('Unsupported backup operation')

    def upload(self, op, args):
        with self.lock:
            # Expired partial transfers are reclaimed; enforce a bounded staging quota.
            for token,row in list(self.uploads.items()):
                if time.time()-row['updated']>3600:
                    row['path'].unlink(missing_ok=True);del self.uploads[token]
            if op == 'upload.begin':
                size=args['size'];digest=args['sha256']
                if not isinstance(size,int) or not 0<size<=8*1024**3 or not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('Invalid upload bounds or hash')
                if len(self.uploads)>=4 or sum(row['size'] for row in self.uploads.values())+size>12*1024**3:raise RuntimeError('Agent upload quota reached')
                if shutil.disk_usage(self.root).free<size+256*1024**2:raise RuntimeError('Insufficient agent staging space')
                token=secrets.token_hex(16);path=self.root/(token+'.upload');path.touch(mode=0o600)
                self.uploads[token]={'path':path,'size':size,'sha256':digest,'offset':0,'updated':time.time(),'complete':False}
                self.busy()
                return {'upload':token}
            row=self.uploads[args['upload']]
            if op == 'upload.chunk':
                data=base64.b64decode(args['data'],validate=True)
                offset=args['offset']
                if len(data)>LIMIT or offset!=row['offset'] or offset+len(data)>row['size'] or row['complete']:raise ValueError('Invalid upload chunk or offset')
                with row['path'].open('ab') as stream:stream.write(data)
                row.update(offset=offset+len(data),updated=time.time())
                return {'offset':row['offset']}
            if op == 'upload.finish':
                with row['path'].open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                if row['offset']!=row['size'] or digest!=row['sha256']:raise ValueError('Upload checksum mismatch')
                row['complete']=True
                return {'verified':True}
            raise ValueError('Unsupported upload operation')

    def release_upload(self, token):
        with self.lock:
            row=self.uploads.pop(token)
            row['path'].unlink(missing_ok=True)
            self.busy()

    def completed_upload(self, token):
        with self.lock:
            row=self.uploads[token]
            if not row['complete']:raise ValueError('Upload not verified')
            return row['path']

    def extract(self, token, work):
        path=self.completed_upload(token);total=0;count=0
        work.mkdir(parents=True,mode=0o755)
        with tarfile.open(path) as archive:
            for member in archive:
                name=Path(member.name);total+=member.size;count+=1
                if not member.isfile() or name.is_absolute() or '..' in name.parts or total>2*1024**3 or count>100000:raise ValueError('Unsafe or oversized snapshot')
                target=work/name
                if target.exists():raise ValueError('Duplicate snapshot entry')
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                with archive.extractfile(member) as source,target.open('wb') as destination:shutil.copyfileobj(source,destination)
                target.chmod(0o755 if member.mode&0o111 else 0o644)
        self.release_upload(token)

    def start(self, args):
        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
        ident=args['id'];folder=self.folder(ident)
        self.cleanup_workspaces()
        with self.lock:
            if ident in self.jobs:return {'status':self.jobs[ident]['status'],'id':ident}
            if shutil.disk_usage(self.root).free<MIN_FREE_BYTES:raise RuntimeError('Worker needs at least 10 GiB free disk space after cleanup')
            if folder.exists():raise ValueError('Run ID already exists; use a new run')
            preparing=args['manifest'].get('prepare_only') is True
            count=sum(j['status']=='running' and bool(j.get('prepare_only'))==preparing for j in self.jobs.values())
            if count >= (1 if preparing else 8):raise RuntimeError('Worker session limit reached')
            work=folder/'source'
            manifest=args['manifest']
            repositories=manifest.get('repositories',{})
            if not {'.','OrgPortal','PIdP','CodeCollective'}.issubset(repositories) or len(repositories)>24:raise ValueError('Expected the application GitHub repositories and bounded submodules')
            for name in repositories:
                if name not in ('.','OrgPortal','PIdP','CodeCollective') and (Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith(('OrgPortal/','PIdP/','CodeCollective/'))):raise ValueError('Invalid nested repository path')
            for repo in repositories.values():
                if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git',repo.get('url','')) or not re.fullmatch('[a-f0-9]{40}',repo.get('commit','')):raise ValueError('Invalid GitHub repository or commit')
            refs={name:{key:repo[key] for key in ('url','commit')} for name,repo in repositories.items()}
            digest=hashlib.sha256(json.dumps(refs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if manifest.get('source_sha256')!=digest:raise ValueError('GitHub source manifest checksum mismatch')
            index=args['index']
            if not isinstance(index,int) or not 0<=index<len(manifest['hosts']):raise ValueError('Invalid shard index')
            target=manifest['hosts'][index]
            config=json.loads(self.config.read_text())
            if manifest['id']!=ident or target['host']!=config['name']:raise ValueError('GitHub manifest targets a different node or run')
            if not preparing:
                # Explicit test work takes priority over speculative installers.
                for key,previous in self.jobs.items():
                    if previous['status']=='running' and previous.get('prepare_only'):
                        (self.folder(key)/'STOP').touch()
                self.changed.notify_all()
            folder.mkdir(mode=0o700)
            (folder/'manifest.json').write_text(json.dumps(manifest))
            job={'status':'running','prepare_only':preparing,'events':collections.deque(maxlen=10000),'sequence':0}
            job.update(source_sha256=digest,images=target.get('images',{}))
            self.jobs[ident]=job
            with self.changed:self.changed.notify_all()
            self.busy()
            (folder/'state.json').write_text(json.dumps({'status':'running'}))
            self.active_workers.add(ident)
            threading.Thread(target=self.worker,args=(ident,work,index,job),daemon=True).start()
            return {'id':ident,'status':'running'}

    def checkout(self,work,manifest,env,job):
        def git(path,*args,local=False,required=True):
            if (work.parent/'STOP').exists():raise InterruptedError('GitHub synchronization stopped')
            result=subprocess.run(['git','-c','core.hooksPath=/dev/null',
                                   '-c','protocol.file.allow='+('always' if local else 'never'),
                                   '-c','submodule.recurse=false','-C',str(path),*args],
                                  capture_output=True,text=True,env=env,timeout=180)
            if result.returncode:
                if not required:return None
                raise RuntimeError('Source Git operation failed: '+args[0]+'; check access and the pinned commit')
            return result.stdout.strip()

        if not manifest.get('prepare_only'):
            with self.storage_lock,self.lock:
                for key,previous in self.jobs.items():
                    if previous.get('prepare_only') and previous.get('status')=='completed':
                        source=self.folder(key)/'source'
                        marker=source/'.prepared-packages.json'
                        if not marker.exists():continue
                        prepared=json.loads(marker.read_text())
                        target=manifest['hosts'][next(i for i,h in enumerate(manifest['hosts']) if h['host']==json.loads(self.config.read_text())['name'])]
                        if prepared.get('source_sha256')!=manifest['source_sha256'] or any(prepared.get('images',{}).get(k)!=v for k,v in target['images'].items()):continue
                        if not all((source/name/'.git').exists() for name in ('.','OrgPortal','PIdP','CodeCollective')):continue
                        source.rename(work)
                        (work/'distributed-plan.json').write_text(json.dumps(manifest))
                        previous['status']='consumed'
                        job['sequence']+=1
                        job['events'].append((job['sequence'],{'kind':'source','repository':'.','commit':manifest['repositories']['.']['commit'],'status':'prepared_snapshot','cache_hit':True}))
                        self.changed.notify_all()
                        return

        target=next(host for host in manifest['hosts'] if host['host']==json.loads(self.config.read_text())['name'])
        # The runner always comes from Organization; application repositories are
        # selected by the actual shard, not by the whole fleet's manifest.
        application={'org-api':'OrgPortal','chat':'OrgPortal','web':'OrgPortal',
                     'visual':'OrgPortal','identity':'PIdP','identity-python':'PIdP','usajobs':'CodeCollective'}
        known=set(application)|{'monitor','map','workspace','p2p','swarm-web'}
        needed={'.'}
        for suite in target['suites']:
            suite=suite.split('--',1)[0]
            if suite not in known:raise ValueError('Unknown suite source requirements: '+suite)
            if target.get('jobs',{}).get(suite,{}).get('status')=='reused':continue
            if suite in application:needed.add(application[suite])
        cache_root=self.root/'git-cache'
        cache_root.mkdir(exist_ok=True,mode=0o700)

        def event(name,repo,status,**details):
            with self.lock:
                job['sequence']+=1
                job['events'].append((job['sequence'],{'kind':'source','repository':name,
                    'commit':repo['commit'],'status':status,**details}))
                self.changed.notify_all()

        for name in sorted(needed,key=lambda value:(len(Path(value).parts),value)):
            repo=manifest['repositories'][name];path=work/name
            if path.is_symlink():raise ValueError('Repository path must not be a symlink')
            path.mkdir(parents=True,exist_ok=True,mode=0o755)
            if any(path.iterdir()):raise ValueError('Checkout directory must be fresh: '+name)
            sparse = name == 'CodeCollective'
            identity=hashlib.sha256(repo['url'].encode()).hexdigest()
            cache=cache_root/(identity+'.git')
            if cache.is_symlink():raise ValueError('Git cache must not be a symlink')
            # One cache per repository URL, retained independently of run cleanup.
            # No network operation occurs when the exact pinned commit is present.
            with (cache_root/(identity+'.lock')).open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                # Every writer owns this lock. A leftover Git lock belongs to an
                # interrupted fetch; discard only this disposable source cache.
                if (cache/'shallow.lock').exists():shutil.rmtree(cache)
                if not cache.exists():
                    cache.mkdir(mode=0o700)
                    git(cache,'init','--bare','--quiet')
                # A stopped installer may leave an initialized cache before
                # remote creation. Repair it while holding the writer lock.
                if git(cache,'remote','get-url','origin',required=False) is None:
                    git(cache,'remote','add','origin',repo['url'])
                if git(cache,'remote','get-url','origin')!=repo['url']:
                    raise ValueError('Git cache origin differs from the source manifest')
                if sparse:
                    git(cache,'config','remote.origin.promisor','true')
                    git(cache,'config','remote.origin.partialclonefilter','blob:none')
                    git(cache,'config','uploadpack.allowFilter','true')
                cached=git(cache,'cat-file','-e',repo['commit']+'^{commit}',required=False) is not None
                seeded=False
                if not cached:
                    for previous in sorted(self.root.glob('*/source'),reverse=True)[:30]:
                        source=previous/name
                        if source==path or source.is_symlink() or not (source/'.git').is_dir():continue
                        if git(source,'remote','get-url','origin',required=False)!=repo['url']:continue
                        if git(source,'cat-file','-e',repo['commit']+'^{commit}',required=False) is None:continue
                        git(cache,'fetch','--no-tags','--depth=1',str(source),
                            repo['commit']+':refs/cache/'+repo['commit'],local=True)
                        cached=True;seeded=True;break
                event(name,repo,'cached' if cached else 'fetching',cache_hit=cached,seeded_from_previous_run=seeded)
                if not cached:
                    git(cache,'fetch','--no-tags','--depth=1',*(['--filter=blob:none'] if sparse else []),'origin',
                        repo['commit']+':refs/cache/'+repo['commit'])
                if git(cache,'rev-parse',repo['commit']+'^{commit}')!=repo['commit']:
                    raise ValueError('Git cache returned a different commit')
                # Local cloning hardlinks immutable objects where supported. Each
                # run owns its refs/index/worktree and has no external alternates.
                git(work,'clone','--local','--no-checkout',str(cache),str(path),local=True)
                # Cache commits live under refs/cache, which clone does not advertise.
                # Fetch the exact object locally so a shallow cache can materialize it.
                if sparse:
                    # Git can disable its local hardlink optimization for partial
                    # repositories. Retain already-cached immutable objects too.
                    for source_object in (cache/'objects').rglob('*'):
                        if not source_object.is_file():continue
                        destination=path/'.git/objects'/source_object.relative_to(cache/'objects')
                        if destination.exists():continue
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        os.link(source_object,destination)
                    git(path,'config','remote.origin.promisor','true')
                    git(path,'config','remote.origin.partialclonefilter','blob:none')
                git(path,'fetch','--no-tags','--depth=1',*(['--filter=blob:none'] if sparse else []),'origin' if sparse else str(cache),repo['commit'],local=True)
            git(path,'remote','set-url','origin',repo['url'])
            if sparse:
                git(path,'sparse-checkout','set','--no-cone','/run.py','/usa/','/css/','/js/reusableComponents.js','/tests/usajobs-mobile.test.cjs')
            # Only missing sparse blobs are fetched from the verified GitHub origin.
            git(path,'checkout','--detach','--force',repo['commit'])
            if git(path,'rev-parse','HEAD')!=repo['commit'] or git(path,'status','--porcelain'):
                raise ValueError('Checkout verification failed: '+name)
            event(name,repo,'verified',cache_hit=cached)
        (work/'distributed-plan.json').write_text(json.dumps(manifest))

    def worker(self, ident, work, index, job):
        folder=work.parent
        try:
            home=self.root/'home';home.mkdir(exist_ok=True,mode=0o700)
            env=worker_environment(home)
            env['SWARM_RESOURCE_DIR']=str(Path(os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources'))))
            manifest=json.loads((folder/'manifest.json').read_text())
            self.checkout(work,manifest,env,job)
            if (folder/'STOP').exists():raise InterruptedError('Run stopped during GitHub synchronization')
            if manifest.get('prepare_only'):
                # Older pinned runners may not implement preparation-only mode.
                # Cache their Git objects, but never accidentally execute tests.
                if manifest.get('sources_only') or 'PREPARATION_PROTOCOL = 1' not in (work/'swarm-control/server/distributed_worker.py').read_text():
                    job.update(status='completed',preparation='sources_only');return
                while True:
                    if (folder/'STOP').exists() or Path('/tmp/launcher-agent-draining').exists():raise InterruptedError('Preparation superseded or launcher updating')
                    with self.lock:
                        busy=any(j is not job and j['status']=='running' and not j.get('prepare_only') for j in self.jobs.values()) or bool(self.browsers)
                    if not busy:break
                    time.sleep(.5)
            process=subprocess.Popen([sys.executable,str(work/'swarm-control/server/distributed_worker.py'),str(index)],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',env=env,start_new_session=True)
            def expire():
                (folder/'STOP').touch()
                self.remove_containers(ident)
                if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
            timer=threading.Timer(7200,expire);timer.start()
            try:
                for line in process.stdout:
                    if len(line)>16000:continue
                    try:event=json.loads(line)
                    except ValueError:event={'kind':'log','line':line[:2000]}
                    with self.lock:
                        if event.get('kind')=='prepared':job['preparation']='ready'
                        job['sequence']+=1;job['events'].append((job['sequence'],event));self.changed.notify_all()
                code=process.wait(timeout=30)
            finally:timer.cancel()
            job.update(status='stopped' if (folder/'STOP').exists() else 'completed' if code==0 else 'failed',exit_code=code)
        except InterruptedError as error:job.update(status='stopped',error=str(error)[:800])
        except Exception as error:job.update(status='failed',error=type(error).__name__+': '+str(error)[:800])
        finally:
            try:self.remove_containers(ident)
            except Exception as error:job.update(status='failed',error='Test container cleanup failed: '+str(error)[:400])
            with self.changed:self.changed.notify_all()
            self.busy()
            job['finished_at']=time.time()
            (folder/'state.json').write_text(json.dumps({key:value for key,value in job.items() if key!='events'}))
            with self.lock:self.active_workers.discard(ident)
            self.cleanup_workspaces()

    def p2p_start(self,args):
        ident=args['id'];self.folder(ident) # Validate before admitting a reservation.
        with self.lock:
            leases=getattr(self,'p2p_leases',None)
            if leases is None:self.p2p_leases={};leases=self.p2p_leases
            if ident not in leases:
                leases[ident]=ResourceLease(ident,2,2048,'orgportal-test-'+ident+'-p2p',
                    directory=os.getenv('SWARM_RESOURCE_DIR',str(self.root/'.resources')))
            lease=leases[ident]
            if not lease.try_acquire():return {'waiting_capacity':True}
        try:return self.start_p2p_browser(args)
        except Exception:
            with self.lock:leases.pop(ident,None);lease.close()
            raise

    def start_p2p_browser(self,args):
        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
        ident=args['id'];folder=self.folder(ident+'-p2p')
        if (self.folder(ident)/'STOP').exists():raise RuntimeError('Run was stopped')
        if folder.exists():raise ValueError('Browser already prepared for this run')
        if self.browsers:raise RuntimeError('An agent P2P browser is already active')
        self.extract(args['upload'],folder)
        if set(p.name for p in folder.iterdir())!={'remote.py','peer.js'}:raise ValueError('Unexpected P2P fixture files')
        # A native agent may use umask 077; these public fixtures must be
        # readable by Selenium's distinct container UID, including rootless UID maps.
        folder.chmod(0o755)
        with self.client() as client:
            image=client.images.get(args['image'])
            if image.id!=args['image']:raise ValueError('P2P requires immutable image ID')
            container=client.containers.run(image.id, ['/p2p/remote.py'],entrypoint='/usr/bin/python3',
                detach=True,name='orgportal-test-'+ident+'-p2p',network_mode='host',nano_cpus=1500000000,
                mem_limit='2g',pids_limit=512,user='1200:1201',shm_size='256m',cap_drop=['ALL'],
                volumes={str(folder):{'bind':'/p2p','mode':'ro'}},environment={
                    'SE_OPTS':'--host 127.0.0.1 --port 4449','SE_START_VNC':'false','SE_START_NO_VNC':'false',
                    'SE_START_XVFB':'false','SE_NODE_MAX_SESSIONS':'1'})
        if (self.folder(ident)/'STOP').exists():
            self.remove_containers(ident);raise RuntimeError('Run stopped during browser startup')
        with self.lock:self.browsers.add(ident);self.busy()
        (folder/'browser.json').write_text(json.dumps({'container':container.id}))
        timer=threading.Timer(1800,lambda:self.dispatch('p2p.stop',{'id':ident}))
        timer.daemon=True;self.browser_timers[ident]=timer;timer.start()
        return {'started':True}

    def hardware(self, info=None):
        if self.hardware_cache is None or time.time()-self.hardware_sampled>15:
            self.hardware_cache=hardware_sample(disk=self.root)
            self.hardware_sampled=time.time()
        if info is not None:self.cuda_runtime='nvidia' in info.get('Runtimes',{})
        value={**self.hardware_cache,'gpu_validation':self.gpu_validation,
               'cuda_container_runtime':getattr(self,'cuda_runtime',None)}
        return value

    def close(self):
        self.telemetry.stopped.set()
        self.storage_stop.set()
        for ident in list(getattr(self,'resource_leases',{})):self.expire_resource(ident)
        for timer in self.browser_timers.values():timer.cancel()
        for ident in list(self.browsers):self.remove_containers(ident)
        for lease in getattr(self,'p2p_leases',{}).values():lease.close()
        for ident,job in list(self.jobs.items()):
            if job['status']=='running':
                (self.folder(ident)/'STOP').touch()
                self.remove_containers(ident)
