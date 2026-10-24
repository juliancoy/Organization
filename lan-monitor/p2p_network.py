"""Real cross-machine WebRTC checks. SSH carries control and one initial seed only."""
import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import io
import ipaddress
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from distributed_images import ImageDistributor

ROOT=Path(__file__).resolve().parents[1]
IMAGE='selenium/standalone-chrome:latest'
URL='https://organization-archive-peers.jcloiacon.workers.dev'
KEY=Path.home()/'.config/organization-peers/signing.key'


def execute(args,**kwargs):
    result=subprocess.run(args,capture_output=True,timeout=kwargs.pop('timeout',60),**kwargs)
    if result.returncode:raise RuntimeError(Path(args[0]).name+' failed; private subprocess output withheld')
    return result.stdout


def direct_evidence(links,participants,local_host):
    """A socket opening or a same-machine browser pair is insufficient evidence."""
    if len({p['machine'] for p in participants})!=len(participants) or len(participants)<2:
        raise RuntimeError('At least two distinct SSH machine identities are required')
    peers={p['peer']:p for p in participants if p.get('peer')};found=[]
    for link in links:
        remote=peers.get(link['peer'])
        if not remote or remote['host']==local_host or link.get('state')!='connected':continue
        try:
            pair=link.get('pair')
            if not pair or pair.get('state')!='succeeded':raise RuntimeError('Selected ICE candidate pair is unavailable')
            if pair['local'].get('candidateType')=='relay' or pair['remote'].get('candidateType')=='relay':
                raise RuntimeError('TURN relay cannot satisfy the direct P2P test')
            for candidate,host in [(pair['local'],next(p for p in participants if p['host']==local_host)),(pair['remote'],remote)]:
                if candidate.get('candidateType') not in {'host','srflx','prflx'} or candidate.get('protocol') not in {'udp','tcp'}:
                    raise RuntimeError('Direct ICE candidate type or protocol is unavailable')
                try:address=ipaddress.ip_address(candidate.get('address','').split('%')[0])
                except ValueError:raise RuntimeError('ICE address is hidden; direct network path cannot be verified') from None
                if address.is_loopback or address.is_unspecified:raise RuntimeError('Loopback ICE candidate cannot satisfy a network test')
                if candidate.get('candidateType')=='host' and str(address) not in host['addresses']:
                    raise RuntimeError('Selected host candidate is not on the attested machine physical interfaces')
            if pair.get('bytesReceived',0)<=0 and pair.get('bytesSent',0)<=0:raise RuntimeError('No bytes observed on selected ICE path')
        except RuntimeError:
            continue  # Only attested routes contribute to transfer evidence.
        found.append(link)
    if not found:raise RuntimeError('No connected cross-machine data channel')
    return found


STATS='''const result=[];for(const [id,link] of peer.links){
 const stats=await link.pc.getStats();let pair;
 for(const row of stats.values())if(row.type==='transport'&&row.selectedCandidatePairId)pair=stats.get(row.selectedCandidatePairId);
 const candidate=c=>c?({address:c.address,port:c.port,protocol:c.protocol,candidateType:c.candidateType}):null;
 const channels=Array.from(stats.values()).filter(r=>r.type==='data-channel').map(r=>({state:r.state,label:r.label,bytesSent:r.bytesSent,bytesReceived:r.bytesReceived}));
 result.push({peer:id,state:link.pc.connectionState,channels,pair:pair?{state:pair.state,bytesSent:pair.bytesSent,bytesReceived:pair.bytesReceived,
 local:candidate(stats.get(pair.localCandidateId)),remote:candidate(stats.get(pair.remoteCandidateId))}:null});}return result;'''


class Browser:
    """Minimal W3C WebDriver client; no host Python/Node dependency installation."""
    def __init__(self,base):
        self.base=base;self.session=None
        value=self.call('/session',{'capabilities':{'alwaysMatch':{'browserName':'chrome','goog:chromeOptions':{'args':[
            '--headless=new','--host-resolver-rules=MAP localhost 127.0.0.2','--no-sandbox','--disable-dev-shm-usage','--disable-features=WebRtcHideLocalIpsWithMdns',
            '--force-webrtc-ip-handling-policy=default_public_and_private_interfaces']}}}})
        self.session=value['sessionId'];self.version=value['capabilities']['browserVersion']
        self.command('/timeouts',{'script':120000,'pageLoad':45000})
        self.command('/url',{'url':'http://localhost:8789/'})
    def call(self,path,data=None):
        request=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=150) as response:result=json.load(response)
        except urllib.error.HTTPError as error:
            result=json.load(error)
        value=result.get('value')
        if isinstance(value,dict) and value.get('error'):raise RuntimeError('WebDriver '+str(value['error']))
        return value
    def command(self,path,data):return self.call('/session/'+self.session+path,data)
    def js(self,body,*args):
        value=self.command('/execute/async',{'script':'''const done=arguments[arguments.length-1];
          (async()=>{'''+body+'''})().then(value=>done({ok:true,value:value??null})).catch(e=>done({ok:false,error:e.message}));''','args':list(args)})
        if not value or not value.get('ok'):raise RuntimeError((value or {}).get('error','Browser script failed'))
        return value['value']
    def wait(self,expression,timeout=45):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            if self.js('return !!('+expression+');'):return
            time.sleep(.25)
        raise RuntimeError('Timed out waiting for browser peer state')
    def close(self):
        if self.session:
            request=urllib.request.Request(self.base+'/session/'+self.session,method='DELETE')
            try:urllib.request.urlopen(request,timeout=10).close()
            except Exception:pass


class NetworkP2P:
    def __init__(self,hosts,ident,ssh,emit=lambda *args:None,stopped=lambda:False,source_archive=None):
        self.hosts=hosts;self.ident=ident;self.ssh=ssh;self.emit=emit;self.stopped=stopped
        self.source_archive=source_archive;self.resources=[];self.report={'status':'running','coverage':{'direct':'pending','cross_nat':'not_measured','turn':'unavailable'},'checks':[],'participants':[]}
    def check_stop(self):
        if self.stopped():raise RuntimeError('P2P run stopped')
    def log(self,host,line):self.emit(host,{'kind':'log','suite':'p2p','phase':'network','line':line})
    def source(self,name):
        if self.source_archive:
            with tarfile.open(self.source_archive) as archive:return archive.extractfile(name).read()
        return (ROOT/name).read_bytes()
    def attest(self,host):
        code='''import hashlib,json,pathlib,socket,subprocess
rows=json.loads(subprocess.check_output(['ip','-j','addr']))
addresses=[a['local'].split('%')[0] for row in rows if row['ifname'].startswith(('en','eth','wl')) for a in row.get('addr_info',[]) if a.get('scope')!='host']
print(json.dumps({'hostname':socket.gethostname(),'machine':hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest(),'addresses':addresses,'clock':__import__('time').time()}))
'''
        value=json.loads(execute(self.ssh(host)+['python3 -'],input=code.encode(),timeout=25))
        if not value['addresses']:raise RuntimeError('No physical network interface addresses on '+host['host'])
        value['clock_skew_seconds']=round(value.pop('clock')-time.time(),3)
        if abs(value['clock_skew_seconds'])>60:raise RuntimeError(host['host']+' clock differs by more than 60 seconds; synchronize time before device invitations can work')
        return {**value,'host':host['host']}
    def prepare(self,host,source):
        self.check_stop();self.log(host,'Preparing isolated browser on '+host['host'])
        remote='/home/'+host['user']+'/organization-test-runs/'+self.ident+'-p2p'
        name='orgportal-test-'+self.ident+'-p2p'
        resource={'host':host,'remote':remote,'name':name,'tunnel':None,'browser':None};self.resources.append(resource)
        execute(self.ssh(host)+['mkdir -p -m 755 '+shlex.quote(remote)],timeout=25)
        with source.open('rb') as stream:execute(self.ssh(host)+['tar -xz -C '+shlex.quote(remote)],stdin=stream,timeout=60)
        # Only public SDK code is mounted. Invitations stay in browser memory and SSH control.
        docker=['docker','run','--pull=never','-d','--name',name,'--network','host','--cpus','1.5','--memory','2g','--pids-limit','512',
            '--user','1200:1201','--shm-size','256m','--cap-drop','ALL',
            '-e','SE_OPTS=--host 127.0.0.1 --port 4449','-e','SE_START_VNC=false','-e','SE_START_NO_VNC=false','-e','SE_START_XVFB=false',
            '-e','SE_NODE_MAX_SESSIONS=1','--mount','type=bind,src='+remote+',dst=/p2p,readonly',
            '--entrypoint','/usr/bin/python3',self.report['browser_images'][host['host']][IMAGE],'/p2p/remote.py']
        execute(self.ssh(host)+[shlex.join(docker)],timeout=40)
        with socket.socket() as available:available.bind(('127.0.0.1',0));port=available.getsockname()[1]
        if host.get('local'):
            port=4449
        else:
            args=self.ssh(host)
            resource['tunnel']=subprocess.Popen(args[:-1]+['-N','-o','ExitOnForwardFailure=yes','-L',f'127.0.0.1:{port}:127.0.0.1:4449',args[-1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base='http://127.0.0.1:'+str(port);until=time.monotonic()+75
        while time.monotonic()<until:
            self.check_stop()
            if resource['tunnel'] and resource['tunnel'].poll() is not None:raise RuntimeError('SSH browser tunnel failed')
            try:
                with urllib.request.urlopen(base+'/status',timeout=2) as response:
                    if json.load(response)['value']['ready']:break
            except (OSError,ValueError):pass
            time.sleep(.5)
        else:raise RuntimeError('Isolated Selenium browser did not become ready on '+host['host'])
        browser=Browser(base);resource['browser']=browser;self.log(host,'Browser ready: Chrome '+browser.version)
        return resource
    def connect(self,resource,secret,room):
        # Load the bounded enrollment helper without placing its key in a worker snapshot.
        import sys
        sys.path.insert(0,str(ROOT/'scripts'))
        from peer_archive import ticket
        claims,token=ticket(secret,room,1800)
        value=resource['browser'].js('''const {ArchivePeer,sha256}=await import('/peer.js');
          window.socketEvents=[];const NativeSocket=window.WebSocket;
          window.WebSocket=class extends NativeSocket {constructor(...args){super(...args);
            for(const name of ['open','error','close'])this.addEventListener(name,e=>socketEvents.push({event:name,code:e.code}));}};
          window.peer=new ArchivePeer({url:arguments[0],token:arguments[1]});window.peerErrors=[];
          peer.addEventListener('error',e=>peerErrors.push(e.detail));
          try{await peer.connect();}catch(e){throw Error(e.message+'; socket events '+JSON.stringify(socketEvents));}
          return {sdk:await sha256(await (await fetch('/peer.js')).arrayBuffer()),relayAvailable:peer.relayAvailable};''',URL,token)
        if value['sdk']!=self.sdk_hash:raise RuntimeError('Remote browser loaded a different SDK revision')
        participant=next(p for p in self.report['participants'] if p['host']==resource['host']['host'])
        participant.update(peer=claims['peer'],browser=resource['browser'].version,client_sha256=value['sdk'])
    def verify(self,resource,expected,full_transfer=True):
        value=resource['browser'].js('''const {sha256}=await import('/peer.js');
         const file=await peer.file('testing-copy.json');return {size:file.size,sha256:await sha256(await file.arrayBuffer())};''')
        if value!=expected:raise RuntimeError('Received fixture bytes differ from the source')
        links=direct_evidence(resource['browser'].js(STATS),self.report['participants'],resource['host']['host'])
        if sum(c.get('bytesReceived',0) for link in links for c in link['channels'])<(expected['size'] if full_transfer else 1):
            raise RuntimeError('Data channel counters do not account for received fixture bytes')
        return links
    def cleanup(self):
        for resource in self.resources:
            if resource['browser']:resource['browser'].close()
            if resource['tunnel']:
                resource['tunnel'].terminate()
                try:resource['tunnel'].wait(timeout=5)
                except subprocess.TimeoutExpired:resource['tunnel'].kill()
            command='docker rm -f '+shlex.quote(resource['name'])+' >/dev/null 2>&1; rm -rf -- '+shlex.quote(resource['remote'])
            try:execute(self.ssh(resource['host'])+[command],timeout=30)
            except Exception:self.report.setdefault('cleanup_errors',[]).append(resource['host']['host'])
    def run(self):
        started=time.monotonic()
        try:
            if len(self.hosts)<2:raise RuntimeError('Cross-machine P2P requires at least two saved SSH test devices')
            secret=KEY.read_text().strip()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                self.report['participants']=list(pool.map(self.attest,self.hosts))
            if len({p['machine'] for p in self.report['participants']})!=len(self.hosts):raise RuntimeError('Saved SSH targets resolve to the same machine')
            def image_progress(image,phase,source,destination):
                for host in self.hosts:
                    if not destination or host['host']==destination:self.log(host,'Browser image '+phase+((' from '+source) if source else ''))
            # The shared helper reuses exact image IDs and streams save|gzip|SSH|load.
            # A container image archive is never staged on either machine's disk.
            images=ImageDistributor(self.hosts,self.ssh,self.stopped,image_progress)
            images.ensure(IMAGE,self.hosts)
            self.report['browser_images']=images.host_images
            self.sdk_hash=hashlib.sha256(self.source('peer-worker/public/peer.js')).hexdigest()
            with tempfile.TemporaryDirectory(prefix='distributed-p2p-') as folder:
                folder=Path(folder);source=folder/'browser.tgz'
                with tarfile.open(source,'w:gz') as archive:
                    for path,name in [('peer-worker/public/peer.js','peer.js'),('lan-monitor/p2p_remote.py','remote.py')]:
                        data=self.source(path);info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644;archive.addfile(info,io.BytesIO(data))
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:resources=list(pool.map(lambda host:self.prepare(host,source),self.hosts))
                import sys
                sys.path.insert(0,str(ROOT/'scripts'))
                from peer_archive import package_file
                body=json.dumps({'fixture':'Synthetic distributed P2P test','nonce':uuid.uuid4().hex,'records':__import__('os').urandom(600000).hex()}).encode()
                fixture=folder/'fixture';fixture.write_bytes(body);package=folder/'package';room=package_file(fixture,package,'testing')
                self.report.update(dataset=room,fixture_sha256=hashlib.sha256(body).hexdigest(),fixture_bytes=len(body),client_sha256=self.sdk_hash)
                for resource in resources[:2]:self.connect(resource,secret,room)
                a,b=resources[:2];a['browser'].wait("Array.from(peer.links.values()).some(l=>l.dc?.readyState==='open')")
                files=[{'path':str(file.relative_to(package)),'base64':base64.b64encode(file.read_bytes()).decode()} for file in package.rglob('*') if file.is_file()]
                a['browser'].js('''const files=arguments[0].map(item=>{const file=new File([Uint8Array.from(atob(item.base64),c=>c.charCodeAt(0))],item.path.split('/').pop());
                 Object.defineProperty(file,'webkitRelativePath',{value:'package/'+item.path});return file;});await peer.seed(files);''',files)
                b['browser'].wait('!!peer.manifest');b['browser'].js('await peer.sync();')
                expected={'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
                links=self.verify(b,expected);self.report['checks'].append({'name':'cross_machine_transfer','status':'passed','host':b['host']['host'],'routes':links})
                self.log(b['host'],'Verified '+str(len(body))+' bytes over direct cross-machine WebRTC; SHA-256 '+expected['sha256'])
                a['browser'].js('await peer.forget();peer.disconnect();');a['browser'].wait('peer.socket.readyState===3')
                receivers=resources[2:] or [a]
                reseed=[]
                for receiver in receivers:
                    self.connect(receiver,secret,room);receiver['browser'].wait('!!peer.manifest');receiver['browser'].js('await peer.sync();')
                    routes=self.verify(receiver,expected)
                    reseed.append({'host':receiver['host']['host'],'routes':routes})
                    self.log(receiver['host'],'Verified reseeding with the original seed disconnected')
                self.report['checks'].append({'name':'reseed_original_offline','status':'passed','receivers':reseed})
                # One missing chunk is repaired over the network while all verified chunks remain cached.
                repair=b['browser'].js('''const [hash,size]=peer.parts.entries().next().value;await peer.store.root.removeEntry(hash);
                  let count=0;peer.addEventListener('progress',e=>{count=e.detail.done;});await peer.sync();return {repairedBytes:size,chunks:count};''')
                self.verify(b,expected,full_transfer=False);self.report['checks'].append({'name':'resume_cached_chunks','status':'passed','host':b['host']['host'],**repair})
                mounted=receiver['browser'].js('''const files={};await peer.mountWasm({mkdirTree(){},writeFile(path,bytes){files[path]=bytes}});
                  const name=Array.from(new TextEncoder().encode('fixtureSize'));
                  const binary=new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,2,19,1,3,101,110,118,11,...name,0,0,7,7,1,3,114,117,110,0,0]);
                  const {instance}=await WebAssembly.instantiate(binary,{env:{fixtureSize:()=>files['/fixtures/testing-copy.json'].length}});return instance.exports.run();''')
                if mounted!=len(body):raise RuntimeError('WASM did not consume the received fixture')
                self.report['checks'].append({'name':'wasm_fixture_consumption','status':'passed','host':receiver['host']['host']})
                self.report['coverage']['direct']='passed';self.report['status']='passed'
        except Exception as error:
            self.report.update(status='stopped' if self.stopped() else 'failed',error=type(error).__name__+': '+str(error)[:400])
        finally:
            self.cleanup();self.report['duration_seconds']=round(time.monotonic()-started,2)
            if self.report.get('cleanup_errors'):self.report['status']='failed'
        return self.report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    from distributed import targets,ssh
    ident='p2p-'+uuid.uuid4().hex[:12]
    def log(host,event):print(host['host']+': '+event.get('line',''),flush=True)
    result=NetworkP2P(targets(),ident,ssh,emit=log).run()
    args.report.parent.mkdir(parents=True,exist_ok=True,mode=0o700);args.report.write_text(json.dumps(result,indent=2)+'\n');args.report.chmod(0o600)
    print('Network P2P:',result['status'],'Report:',args.report,flush=True)
    if result.get('error'):print(result['error'],flush=True)
    raise SystemExit(0 if result['status']=='passed' else 1)
