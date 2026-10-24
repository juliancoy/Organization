"""Client for the encrypted launcher job protocol; no SSH fallback."""
import base64
import hashlib
import http.client
import threading
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import secrets
import time

PROTOCOL='lifetech-agent.v1'
CONFIG=Path(os.getenv('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))


class AgentClient:
    def __init__(self, host):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config=json.loads(CONFIG.read_text())
        self.node,self.sender=host['host'],config['name']
        self.endpoint=host['agent_endpoint']
        if self.node not in config['peers']:raise ValueError('Agent is not enrolled')
        self.cipher=AESGCM(bytes.fromhex(config['key']))
        endpoint=urlsplit(self.endpoint)
        if endpoint.scheme not in ('http','https') or endpoint.username or endpoint.password or endpoint.path not in ('','/'):
            raise ValueError('Invalid agent endpoint')
        connection=http.client.HTTPSConnection if endpoint.scheme=='https' else http.client.HTTPConnection
        self.connection=connection(endpoint.hostname,endpoint.port,timeout=30)
        self.connection_lock=threading.Lock()

    def call(self, op, args=None, timeout=30):
        request_id=secrets.token_hex(16)
        value={'from':self.sender,'to':self.node,'time':time.time(),'nonce':request_id,'op':op,'args':args or {}}
        nonce=secrets.token_bytes(12)
        data=nonce+self.cipher.encrypt(nonce,json.dumps(value).encode(),PROTOCOL.encode())
        with self.connection_lock:
            try:
                self.connection.timeout=timeout
                if self.connection.sock:self.connection.sock.settimeout(timeout)
                self.connection.request('POST','/agent/v1',body=data,headers={'Content-Type':'application/octet-stream','X-Lifetech-Peer':self.node})
                response=self.connection.getresponse()
                raw=response.read(8*1024**2+1)
                if response.status!=200:raise RuntimeError('Agent HTTP status '+str(response.status))
            except Exception:
                self.connection.close()
                raise
        if len(raw)>8*1024**2:raise RuntimeError('Agent response exceeds limit')
        reply=json.loads(self.cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
        if reply.get('nonce')!=request_id or reply.get('node')!=self.node or abs(time.time()-reply['time'])>60:raise RuntimeError('Agent response identity or freshness mismatch')
        if not reply.get('ok'):raise RuntimeError(reply.get('error','Agent operation failed'))
        return reply['result']

    def close(self):
        with self.connection_lock:self.connection.close()

    def upload(self,path,stopped=lambda:False,max_bytes_per_second=None):
        if max_bytes_per_second is not None and max_bytes_per_second<=0:raise ValueError('Upload rate must be positive')
        path=Path(path)
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        token=self.call('upload.begin',{'size':path.stat().st_size,'sha256':digest})['upload']
        offset=0;started=time.monotonic()
        with path.open('rb') as stream:
            while data:=stream.read(1024*1024):
                if stopped():raise InterruptedError('Agent upload stopped')
                result=self.call('upload.chunk',{'upload':token,'offset':offset,'data':base64.b64encode(data).decode()},timeout=120)
                offset+=len(data)
                if result['offset']!=offset:raise RuntimeError('Agent upload offset mismatch')
                if max_bytes_per_second:
                    delay=offset/max_bytes_per_second-(time.monotonic()-started)
                    if delay>0:time.sleep(delay)
        self.call('upload.finish',{'upload':token},timeout=180)
        return token


def wait_for_events(client,ident,cursor,stopped=lambda:False):
    """Resume by sequence after transport loss; never replay a mutation."""
    for attempt in range(3):
        if stopped():raise InterruptedError('Event wait stopped')
        try:
            return client.call('job.poll',{'id':ident,'cursor':cursor,'wait_seconds':20},timeout=25)
        except (OSError,http.client.HTTPException):
            if attempt==2:raise
            time.sleep(attempt+1)


class AgentSessions:
    def __init__(self,root,hosts,stopped,progress):
        self.root,self.hosts,self.stopped,self.progress=Path(root),hosts,stopped,progress
        self.expected_revision=None
        self.updater=None

    def reject(self,host,reason):
        host.update(status='unavailable',error=str(reason)[:800])
        host['session']={'status':'unavailable','verified':False,'error':str(reason)[:800]}
        self.progress(host,'unavailable',host['session'])

    def accessible(self):
        import sys,ipaddress,subprocess
        sys.path.insert(0,str(self.root/'scripts'))
        from zeroconf import ServiceBrowser,Zeroconf,IPVersion
        from launcher_discovery import SERVICE
        config=json.loads(CONFIG.read_text())
        address=config['peers'][config['name']]
        rows=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','enp7s0'],text=True,timeout=5))
        networks=[ipaddress.ip_network(a['local']+'/'+str(a['prefixlen']),strict=False) for row in rows for a in row.get('addr_info',[]) if a.get('scope')=='global']
        if not any(address==a['local'] for row in rows for a in row.get('addr_info',[])):raise RuntimeError('Enrolled coordinator address is not on Ethernet enp7s0')
        found={}
        class Listener:
            def add_service(inner,zc,kind,name):inner.update_service(zc,kind,name)
            def remove_service(inner,zc,kind,name):found.pop(name,None)
            def update_service(inner,zc,kind,name):
                info=zc.get_service_info(kind,name,timeout=1500)
                if not info:return
                node=info.properties.get(b'node',b'').decode(errors='replace')
                if node not in config['peers'] or info.port!=8790:return
                if info.properties.get(b'agent')!=PROTOCOL.encode():return
                if config['peers'][node] not in info.parsed_addresses(IPVersion.V4Only):return
                found[name]=(node,'http://'+config['peers'][node]+':8790')
        zc=Zeroconf(interfaces=[address],ip_version=IPVersion.V4Only)
        browser=ServiceBrowser(zc,SERVICE,listener=Listener())
        try:
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                if self.stopped():raise InterruptedError('Agent discovery stopped')
                time.sleep(.1)
            endpoints=dict(list(found.values()))
        finally:browser.cancel();zc.close()
        reachable=[]
        for host in self.hosts:
            enrolled=config['peers'][host['host']]
            if not any(ipaddress.ip_address(enrolled) in network for network in networks):
                self.reject(host,'Enrolled agent endpoint is outside the coordinator Ethernet LAN');continue
            host['discovery']='DNS-SD' if host['host'] in endpoints else 'Enrolled endpoint; DNS-SD announcement absent'
            host.update(agent_endpoint=endpoints.get(host['host'],'http://'+enrolled+':8790'),lan_address=enrolled)
            reachable.append(host)
        return reachable

    def verify(self,hosts):
        if self.stopped():raise InterruptedError('Agent readiness stopped')
        if not hosts:return []
        import concurrent.futures
        if self.updater is None:
            from agent_updates import LauncherUpdates
            self.updater=LauncherUpdates(self.root,self.stopped,self.progress)
            self.expected_revision=self.updater.revision
        expected=self.expected_revision
        def probe(host):
            if self.stopped():raise InterruptedError('Agent readiness stopped')
            try:
                client=AgentClient(host)
                try:value=client.call('status',timeout=5)
                except (OSError,TimeoutError):
                    client.close()
                    host['agent_endpoint']='http://127.0.0.1:8791'
                    host['transport']='webrtc'
                    client=AgentClient(host);value=client.call('status',timeout=30)
                if value.get('protocol')!=PROTOCOL:raise RuntimeError('Agent protocol mismatch')
                if host.get('transport')!='webrtc' and host['lan_address'] not in value.get('addresses',[]):raise RuntimeError('Agent address is not on an attested physical interface')
                if value.get('worker_policy',{}).get('accepting_jobs') is False:
                    host['worker_policy']=value['worker_policy']
                    raise RuntimeError(value['worker_policy']['reason'])
                if value.get('active_jobs'):raise RuntimeError('Agent is busy with an existing job')
                value=self.updater.ensure(host,client,value)
                required={'github-jobs','p2p-browser','cancel','events','layer-pull','registry-read','file-queue','snapshot-stage','event-wait'}
                if value.get('protocol')!=PROTOCOL:raise RuntimeError('Agent protocol mismatch after launcher update')
                if not value.get('ready'):raise RuntimeError('Agent is not ready or is draining for an update')
                missing=required-set(value.get('capabilities',[]))
                if missing:raise RuntimeError('Agent capabilities missing: '+', '.join(sorted(missing))+' after launcher update')
                if value.get('source_revision')!=expected:raise RuntimeError('Launcher update revision verification failed')
                if value.get('worker_policy',{}).get('accepting_jobs') is False:
                    host['worker_policy']=value['worker_policy']
                    raise RuntimeError(value['worker_policy']['reason'])
                if value.get('active_jobs'):raise RuntimeError('Agent is busy with an existing job')
                if host.get('transport')!='webrtc' and host['lan_address'] not in value['addresses']:raise RuntimeError('Agent address is not on an attested physical interface')
                host['session']={'status':'ready','verified':True,'verified_at':time.time(),'transport':PROTOCOL,'machine':value['machine'],'capabilities':value['capabilities']}
                host['agent_status']=value
                self.progress(host,'ready',host['session'])
                return host
            except InterruptedError:raise
            except Exception as error:self.reject(host,str(error));return None
        if not hosts:return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8,len(hosts))) as pool:ready=[h for h in pool.map(probe,hosts) if h]
        unique={};result=[]
        for host in ready:
            identity=host['session']['machine']
            if identity in unique:self.reject(host,'Duplicate physical machine identity: '+unique[identity])
            else:unique[identity]=host['host'];result.append(host)
        return result
