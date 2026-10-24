"""Authenticated, bounded LAN job protocol. No shell-command endpoint."""
import base64
import collections
import contextlib
import hashlib
import json
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

VERSION = 'lifetech-agent.v1'
LIMIT = 1024 * 1024
METRIC_SAMPLE = "import json,time,os,socket,subprocess\nfrom pathlib import Path\ndef read():\n result={}\n for line in Path('/proc/stat').read_text().splitlines():\n  fields=line.split()\n  if fields and (fields[0]=='cpu' or fields[0].startswith('cpu') and fields[0][3:].isdigit()):\n   values=[int(v) for v in fields[1:9]]\n   result[fields[0]]=(sum(values),values[3]+values[4])\n return result\ndef io():\n result={}\n for device in Path('/sys/block').iterdir():\n  if device.resolve().as_posix().startswith('/sys/devices/virtual/'):continue\n  try:\n   v=(device/'stat').read_text().split();result[device.name]=(int(v[9]),int(v[2])+int(v[6]))\n  except (OSError,ValueError,IndexError):pass\n return result\ndef net():\n result=0\n for device in Path('/sys/class/net').iterdir():\n  if device.name=='lo' or '/virtual/' in str(device.resolve()):continue\n  try:result+=sum(int((device/'statistics'/field).read_text()) for field in ['rx_bytes','tx_bytes'])\n  except (OSError,ValueError):pass\n return result\na=read();ia=io();na=net();start=time.monotonic();time.sleep(1);b=read();ib=io();nb=net();elapsed=time.monotonic()-start\ndef utilization(key):\n if key not in a:return None\n total=b[key][0]-a[key][0];idle=b[key][1]-a[key][1]\n return round(max(0,min(100,100*(total-idle)/total)),1) if total>0 else None\ncores=[{'id':int(key[3:]),'percent':utilization(key)} for key in b if key!='cpu']\nmemory={}\nfor line in Path('/proc/meminfo').read_text().splitlines():\n k,v=line.split(':',1);memory[k]=int(v.split()[0])*1024\nram_total=memory.get('MemTotal',0);ram_used=max(0,ram_total-memory.get('MemAvailable',ram_total))\ndisks=[max(0,min(100,(ib[k][0]-ia[k][0])/elapsed/10)) for k in ib if k in ia]\ntry:ips=subprocess.check_output(['hostname','-I'],text=True,timeout=1).split()\nexcept (OSError,subprocess.SubprocessError):ips=[]\nprint(json.dumps({'percent':utilization('cpu'),'cpus':len(cores),'logical_cpus':cores,\n 'observed_hostname':socket.gethostname(),'addresses':ips,\n 'ram_percent':round(100*ram_used/ram_total,1) if ram_total else None,\n 'ram_used_bytes':ram_used,'ram_total_bytes':ram_total,\n 'disk_busy_percent':round(max(disks),1) if disks else None,\n 'disk_basis':'Busiest physical block device; Linux I/O busy time',\n 'network_mbps':round(max(0,nb-na)*8/elapsed/1000000,4)}))\n"


class Agent:
    def __init__(self, config, root):
        self.config = config
        folder=Path(__file__).resolve().parent
        self.source_revision=hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.nonces = {}
        self.jobs = {}
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
            state = json.loads(path.read_text())
            if state.get('status') in ('running', 'starting'):
                (path.parent/'STOP').touch()
                state.update(status='interrupted', error='Agent restarted during execution')
                path.write_text(json.dumps(state))
                self.remove_containers(path.parent.name)
        states=sorted(self.root.glob('*/state.json'),key=lambda path:path.stat().st_mtime,reverse=True)
        for path in states[10:]:shutil.rmtree(path.parent)

    def busy(self):
        path=Path('/tmp/launcher-agent-busy.json');temporary=path.with_suffix('.tmp')
        with self.lock:
            temporary.write_text(json.dumps({'jobs':[key for key,job in self.jobs.items() if job['status']=='running'],
                                             'browsers':sorted(self.browsers),'transfers':self.registry_transfers,'time':time.time()}));temporary.replace(path)

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
        config = json.loads(self.config.read_text())
        cipher = AESGCM(bytes.fromhex(config['key']))
        try:
            raw = await request.read()
            value = json.loads(cipher.decrypt(raw[:12], raw[12:], VERSION.encode()))
            if (value.get('to') != config['name'] or value.get('from') not in config['peers'] or
                    abs(time.time()-value['time']) > 60 or not re.fullmatch('[a-f0-9]{32}',value['nonce'])):
                raise ValueError('Invalid request identity or freshness')
            with self.lock:
                self.nonces = {n:t for n,t in self.nonces.items() if t > time.time()}
                if value['nonce'] in self.nonces or len(self.nonces)>4096:
                    raise ValueError('Replayed request or request limit')
                self.nonces[value['nonce']] = time.time()+120
            try:
                result = await asyncio.to_thread(self.dispatch, value['op'], value.get('args',{}))
                reply = {'ok':True, 'result':result}
            except Exception as error:
                reply = {'ok':False,'error':type(error).__name__+': '+str(error)[:800]}
            reply.update(nonce=value['nonce'], node=config['name'], time=time.time())
            nonce = secrets.token_bytes(12)
            return web.Response(body=nonce+cipher.encrypt(nonce,json.dumps(reply).encode(),VERSION.encode()),
                                content_type='application/octet-stream')
        except Exception:
            raise web.HTTPForbidden(text='Agent authentication rejected')

    def folder(self, ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}(?:-p2p)?',ident):
            raise ValueError('Invalid run ID')
        return self.root/ident

    def dispatch(self, op, args):
        if op=='registry.read':
            from launcher_registry import read
            return read(args)
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
            result=subprocess.run([sys.executable,'-c',METRIC_SAMPLE],capture_output=True,text=True,timeout=8,check=True)
            return json.loads(result.stdout)
        if op == 'status':
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
                    'capabilities':['github-jobs','p2p-browser','cancel','events','layer-pull','registry-read'],
                    'active_jobs':[key for key,j in self.jobs.items() if j['status']=='running'],'active_image_transfers':self.registry_transfers}
        if op.startswith('upload.'):
            return self.upload(op,args)
        if op == 'image.inspect':
            import docker
            with self.client() as client:
                try:
                    image = client.images.get(args['image'])
                    return {'id':image.id,'os':image.attrs['Os'],'architecture':image.attrs['Architecture'],'fingerprint':self.image_fingerprint(image)}
                except docker.errors.ImageNotFound:return None
        if op == 'job.start':return self.start(args)
        if op == 'job.poll':
            ident=args['id'];folder=self.folder(ident)
            with self.lock:
                job=self.jobs.get(ident)
                if job is None:
                    state=json.loads((folder/'state.json').read_text())
                    return {**state,'events':[],'cursor':0,'events_lost':True}
                cursor=int(args.get('cursor',0))
                events=[{'sequence':n,'event':event} for n,event in job['events'] if n>cursor][:200]
                return {'status':job['status'],'exit_code':job.get('exit_code'),'error':job.get('error'),
                        'events':events,'cursor':events[-1]['sequence'] if events else cursor,
                        'events_lost':bool(job['events'] and cursor<job['events'][0][0]-1)}
        if op == 'job.stop':
            folder=self.folder(args['id']);folder.mkdir(exist_ok=True,mode=0o700)
            (folder/'STOP').touch()
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
            data=json.dumps(args['data']).encode() if args.get('data') is not None else None
            request=urllib.request.Request('http://127.0.0.1:4449'+path,data=data,method=method,headers={'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(request,timeout=150) as response:return json.load(response)
            except urllib.error.HTTPError as error:return json.load(error)
        if op == 'p2p.stop':
            ident=args['id']+'-p2p';folder=self.folder(ident)
            self.remove_containers(args['id'])
            shutil.rmtree(folder,ignore_errors=True)
            with self.lock:
                timer=self.browser_timers.pop(args['id'],None)
                if timer:timer.cancel()
                self.browsers.discard(args['id']);self.busy()
            return {'acknowledged':True}
        raise ValueError('Unsupported agent operation')

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
        with self.lock:
            if ident in self.jobs:return {'status':self.jobs[ident]['status'],'id':ident}
            if folder.exists():raise ValueError('Run ID already exists; use a new run')
            if any(j['status']=='running' for j in self.jobs.values()):raise RuntimeError('Agent already executing a shard')
            work=folder/'source'
            manifest=args['manifest']
            repositories=manifest.get('repositories',{})
            if not {'.','OrgPortal','PIdP'}.issubset(repositories) or len(repositories)>24:raise ValueError('Expected the three GitHub repositories and bounded submodules')
            for name in repositories:
                if name not in ('.','OrgPortal','PIdP') and (Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith(('OrgPortal/','PIdP/'))):raise ValueError('Invalid nested repository path')
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
            for key in list(self.jobs)[:-9]:
                if self.jobs[key]['status']!='running':
                    del self.jobs[key];shutil.rmtree(self.folder(key),ignore_errors=True)
            folder.mkdir(mode=0o700)
            (folder/'manifest.json').write_text(json.dumps(manifest))
            job={'status':'running','events':collections.deque(maxlen=10000),'sequence':0}
            self.jobs[ident]=job
            self.busy()
            (folder/'state.json').write_text(json.dumps({'status':'running'}))
            threading.Thread(target=self.worker,args=(ident,work,index,job),daemon=True).start()
            return {'id':ident,'status':'running'}

    def checkout(self,work,manifest,env,job):
        def git(path,*args):
            if (work.parent/'STOP').exists():raise InterruptedError('GitHub synchronization stopped')
            result=subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','protocol.file.allow=never',
                                   '-c','submodule.recurse=false','-C',str(path),*args],
                                  capture_output=True,text=True,env=env,timeout=180)
            if result.returncode:raise RuntimeError('GitHub '+args[0]+' failed for '+str(path.relative_to(work) or '.')+'; check access and the pinned commit')
            return result.stdout.strip()
        for name in sorted(manifest['repositories'],key=lambda value:(len(Path(value).parts),value)):
            repo=manifest['repositories'][name];path=work/name
            if path.is_symlink():raise ValueError('Repository path must not be a symlink')
            path.mkdir(parents=True,exist_ok=True,mode=0o755)
            if any(path.iterdir()):raise ValueError('GitHub checkout directory must be fresh: '+name)
            with self.lock:
                job['sequence']+=1
                job['events'].append((job['sequence'],{'kind':'source','repository':name,'commit':repo['commit'],'status':'fetching'}))
            git(path,'init','--quiet')
            git(path,'remote','add','origin',repo['url'])
            git(path,'fetch','--no-tags','--depth=1','origin',repo['commit'])
            if git(path,'rev-parse','FETCH_HEAD')!=repo['commit']:raise ValueError('GitHub fetched a different commit')
            git(path,'checkout','--detach','--force',repo['commit'])
            if git(path,'rev-parse','HEAD')!=repo['commit'] or git(path,'status','--porcelain'):raise ValueError('Checkout verification failed: '+name)
            with self.lock:
                job['sequence']+=1
                job['events'].append((job['sequence'],{'kind':'source','repository':name,'commit':repo['commit'],'status':'verified'}))
        (work/'distributed-plan.json').write_text(json.dumps(manifest))

    def worker(self, ident, work, index, job):
        folder=work.parent
        try:
            home=self.root/'home';home.mkdir(exist_ok=True,mode=0o700)
            env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
            env.update(HOME=str(home),PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',GIT_TERMINAL_PROMPT='0',GIT_CONFIG_NOSYSTEM='1')
            manifest=json.loads((folder/'manifest.json').read_text())
            self.checkout(work,manifest,env,job)
            if (folder/'STOP').exists():raise InterruptedError('Run stopped during GitHub synchronization')
            process=subprocess.Popen([sys.executable,str(work/'lan-monitor/distributed_worker.py'),str(index)],
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
                        job['sequence']+=1;job['events'].append((job['sequence'],event))
                code=process.wait(timeout=30)
            finally:timer.cancel()
            job.update(status='stopped' if (folder/'STOP').exists() else 'completed' if code==0 else 'failed',exit_code=code)
        except InterruptedError as error:job.update(status='stopped',error=str(error)[:800])
        except Exception as error:job.update(status='failed',error=type(error).__name__+': '+str(error)[:800])
        finally:
            try:self.remove_containers(ident)
            except Exception as error:job.update(status='failed',error='Test container cleanup failed: '+str(error)[:400])
            self.busy()
            (folder/'state.json').write_text(json.dumps({key:value for key,value in job.items() if key!='events'}))

    def p2p_start(self,args):
        if Path('/tmp/launcher-agent-draining').exists():raise RuntimeError('Agent is draining for an explicit update')
        ident=args['id'];folder=self.folder(ident+'-p2p')
        if (self.folder(ident)/'STOP').exists():raise RuntimeError('Run was stopped')
        if folder.exists():raise ValueError('Browser already prepared for this run')
        if self.browsers:raise RuntimeError('An agent P2P browser is already active')
        self.extract(args['upload'],folder)
        if set(p.name for p in folder.iterdir())!={'remote.py','peer.js'}:raise ValueError('Unexpected P2P fixture files')
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

    def close(self):
        for timer in self.browser_timers.values():timer.cancel()
        for ident in list(self.browsers):self.remove_containers(ident)
        for ident,job in list(self.jobs.items()):
            if job['status']=='running':
                (self.folder(ident)/'STOP').touch()
                self.remove_containers(ident)
