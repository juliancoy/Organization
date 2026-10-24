"""Direct launcher WebRTC status mesh; encrypted LAN signaling, no cloud relay."""
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time

CONFIG = Path.home() / '.config/lifetech-launcher/peers.json'
PORT = 8790
PROTOCOL = 'lifetech-launcher.v1'


class Mesh:
    def __init__(self, config, prefix, agent=None):
        self.agent = agent
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.name, self.peers = config['name'], config['peers']
        import hashlib
        folder = Path(__file__).resolve().parent
        self.config_revision = hashlib.sha256(json.dumps(self.peers,sort_keys=True).encode()).hexdigest()
        self.source_revision = hashlib.sha256(b''.join((folder/name).read_bytes() for name in ('launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'))).hexdigest()
        if self.name not in self.peers or len(self.peers) > 24:
            raise ValueError('Mesh requires 1–24 enrolled launchers including this machine')
        self.cipher = AESGCM(bytes.fromhex(config['key']))
        self.prefix = 'lifetech-' if prefix == 'bmoremedtech-' else prefix
        self.links, self.channels, self.observations, self.nonces = {}, {}, {}, {}
        self.created = {}
        self.runner = None

    def seal(self, message):
        nonce = secrets.token_bytes(12)
        raw = json.dumps(message, separators=(',', ':')).encode()
        return nonce + self.cipher.encrypt(nonce, raw, PROTOCOL.encode())

    def open(self, data):
        if not 28 <= len(data) <= 65536:
            raise ValueError('Invalid signaling size')
        message = json.loads(self.cipher.decrypt(data[:12], data[12:], PROTOCOL.encode()))
        if (message['to'] != self.name or message['from'] not in self.peers
                or message['from'] == self.name or abs(time.time() - message['time']) > 60):
            raise ValueError('Invalid signaling identity or clock')
        return message

    def envelope(self, peer, description, nonce):
        return {'from': self.name, 'to': peer, 'time': time.time(), 'nonce': nonce,
                'type': description.type, 'sdp': description.sdp}

    async def drop(self, peer):
        pc = self.links.pop(peer, None)
        self.channels.pop(peer, None)
        self.observations.pop(peer, None)
        self.created.pop(peer, None)
        if pc:
            await pc.close()

    def attach(self, peer, channel):
        if channel.label != PROTOCOL or peer in self.channels:
            channel.close()
            return
        self.channels[peer] = channel

        @channel.on('message')
        def message(raw):
            if not isinstance(raw, str) or len(raw) > 32768:
                channel.close()
                return
            try:
                value = json.loads(raw)
                if value.get('type')=='agent-rpc' and getattr(self,'gateway',None):
                    self.gateway.receive(peer,value);return
                if value['type'] != 'status' or value['name'] != peer:
                    raise ValueError('Unexpected peer message')
                services = value['services']
                if not isinstance(services, list) or len(services) > 100:
                    raise ValueError('Invalid service list')
                clean = []
                for service in services:
                    if not isinstance(service, dict):
                        raise ValueError('Invalid service')
                    clean.append({key: str(service.get(key, ''))[:200] for key in ('name', 'state')})
                self.observations[peer] = {'received': time.time(), 'services': clean,
                                           'ready': value.get('ready') is True}
            except (ValueError, KeyError, TypeError):
                channel.close()

    def connection(self, peer):
        from aiortc import RTCPeerConnection, RTCConfiguration
        pc = RTCPeerConnection(getattr(self,'rtc_config',RTCConfiguration(iceServers=[])))
        self.links[peer] = pc
        self.created[peer] = time.monotonic()

        @pc.on('datachannel')
        def channel(dc):
            self.attach(peer, dc)

        return pc

    async def offer(self, request):
        from aiohttp import web
        from aiortc import RTCSessionDescription
        try:
            value = self.open(await request.read())
            peer, nonce = value['from'], value['nonce']
            self.nonces = {n: expiry for n, expiry in self.nonces.items() if expiry > time.time()}
            if (value['type'] != 'offer' or peer >= self.name or not isinstance(nonce, str)
                    or len(nonce) != 32 or nonce in self.nonces):
                raise ValueError('Invalid offer')
            self.nonces[nonce] = time.time() + 120
            if len(self.nonces) > 1024:
                raise ValueError('Too many offers')
            # Keep a healthy connection: do not let retries replace it.
            if peer in self.links and self.links[peer].connectionState == 'connected':
                raise ValueError('Peer already connected')
            await self.drop(peer)
            pc = self.connection(peer)
            try:
                async with asyncio.timeout(20):
                    await pc.setRemoteDescription(RTCSessionDescription(value['sdp'], 'offer'))
                    await pc.setLocalDescription(await pc.createAnswer())
            except Exception:
                await self.drop(peer)
                raise
            return web.Response(body=self.seal(self.envelope(peer, pc.localDescription, nonce)),
                                content_type='application/octet-stream')
        except Exception:
            raise web.HTTPBadRequest(text='Peer negotiation rejected')

    async def dial(self, peer, session):
        from aiortc import RTCSessionDescription
        pc = self.connection(peer)
        self.attach(peer, pc.createDataChannel(PROTOCOL))
        nonce = secrets.token_hex(16)
        try:
            async with asyncio.timeout(20):
                await pc.setLocalDescription(await pc.createOffer())
                endpoint = self.discovery.endpoint(peer)
                if endpoint is None:
                    raise ValueError('No current DNS-SD service announcement')
                host, port = endpoint
                url = f'http://{host}:{port}/offer'
                async with session.post(url, data=self.seal(self.envelope(peer, pc.localDescription, nonce)),
                                        allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError('Peer rejected negotiation')
                    raw = await response.content.read(65537)
                value = self.open(raw)
                if value['from'] != peer or value['nonce'] != nonce or value['type'] != 'answer':
                    raise ValueError('Unexpected answer')
                await pc.setRemoteDescription(RTCSessionDescription(value['sdp'], 'answer'))
        except Exception as error:
            await self.drop(peer)
            logging.info('Launcher peer %s unavailable: %s', peer, type(error).__name__)

    async def status(self):
        def collect():
            import docker
            client = docker.from_env(timeout=5)
            try:
                return [{'name': container.name[:200], 'state': container.status[:200]}
                        for container in client.containers.list(all=True,filters={'name':self.prefix})
                        if container.name.startswith(self.prefix)][:100]
            finally:
                client.close()
        services = await asyncio.wait_for(asyncio.to_thread(collect), 8)
        return {'type': 'status', 'name': self.name, 'ready': Path('/tmp/launcher-ready').exists(),
                'services': services[:100]}

    async def run(self, stopped):
        from aiohttp import web, ClientSession, ClientTimeout
        app = web.Application(client_max_size=2*1024**2)
        app.router.add_post('/offer', self.offer)
        if self.agent:app.router.add_post('/agent/v1',self.agent.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        from launcher_discovery import Discovery
        self.discovery = Discovery(self.name, self.peers, PORT)
        await web.TCPSite(self.runner, self.peers[self.name], PORT).start()
        print(f'Launcher WebRTC listening on LAN port {PORT}; {len(self.peers)-1} enrolled peers', flush=True)
        initial = {'name':self.name,'source_revision':self.source_revision,'config_revision':self.config_revision,'peers':{peer:{'connection':'disconnected'} for peer in self.peers if peer!=self.name}}
        path = Path('/tmp/launcher-peers.json')
        temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(initial));temporary.replace(path)
        try:
            await asyncio.to_thread(self.discovery.start)
            if self.agent:
                readiness=await asyncio.to_thread(self.agent.dispatch,'status',{})
                if not readiness['ready']:raise RuntimeError('Agent execution capabilities are unavailable')
                marker=Path('/tmp/launcher-agent-ready.json')
                temporary=marker.with_suffix('.tmp')
                temporary.write_text(json.dumps({'folder':str(Path(__file__).resolve().parent),'time':time.time()}));temporary.replace(marker)
            async with ClientSession(timeout=ClientTimeout(total=20), trust_env=False) as session:
                while not stopped.is_set():
                    for peer, pc in list(self.links.items()):
                        observation = self.observations.get(peer)
                        stale = observation and time.time() - observation['received'] > 45
                        if (pc.connectionState in ('failed', 'closed') or stale
                                or (not observation and time.monotonic()-self.created[peer] > 45)):
                            await self.drop(peer)
                    await asyncio.gather(*(self.dial(peer, session) for peer in self.peers
                                           if self.name < peer and peer not in self.links))
                    try:
                        value = json.dumps(await self.status(), separators=(',', ':'))
                        for channel in list(self.channels.values()):
                            if channel.readyState == 'open' and channel.bufferedAmount < 65536:
                                channel.send(value)
                    except Exception as error:
                        logging.warning('Launcher status unavailable: %s', type(error).__name__)
                    snapshot = {peer: {'connection': self.links[peer].connectionState if peer in self.links else 'disconnected',
                                       **self.observations.get(peer, {})} for peer in self.peers if peer != self.name}
                    path = Path('/tmp/launcher-peers.json')
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'name': self.name, 'source_revision':self.source_revision,'config_revision':self.config_revision, 'peers': snapshot}))
                    temporary.replace(path)
                    for _ in range(10):
                        if stopped.is_set():
                            break
                        await asyncio.sleep(1)
        finally:
            await asyncio.to_thread(self.discovery.close)
            await self.runner.cleanup()
            await asyncio.gather(*(self.drop(peer) for peer in list(self.links)))


def start(prefix):
    import subprocess
    import sys
    supervisor = Path(__file__).with_name('launcher_peer_supervisor.py')
    process = subprocess.Popen([sys.executable,'-B',str(supervisor),'--prefix',prefix])

    def stop():
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
    return stop


class InternetMesh(Mesh):
    """Renew credentials and negotiate WebRTC through the public signaling room."""
    def __init__(self, config, prefix):
        self.registration = config['internet']
        peer = self.registration['peer']
        super().__init__({'name':peer,'peers':{peer:peer},'key':secrets.token_hex(32)},prefix)
        self.socket = None
        self.expires = 0

    async def dial(self, peer, session=None):
        if peer in self.links:
            return
        pc = self.connection(peer)
        self.attach(peer, pc.createDataChannel(PROTOCOL))
        try:
            async with asyncio.timeout(20):
                await pc.setLocalDescription(await pc.createOffer())
                await self.socket.send_json({'type':'offer','to':peer,'description':{
                    'type':'offer','sdp':pc.localDescription.sdp}})
        except Exception:
            await self.drop(peer)
            raise

    async def signal(self, value):
        from aiortc import RTCSessionDescription
        kind = value.get('type')
        if kind == 'welcome':
            if value['peer'] != self.name or value['room'] != self.registration['room']:
                raise ValueError('Signaling identity mismatch')
            for peer in value['peers']:
                self.peers[peer] = peer
                if self.name < peer:
                    await self.dial(peer)
        elif kind == 'joined':
            peer = value['peer'];self.peers[peer] = peer
            if self.name < peer:
                await self.dial(peer)
        elif kind == 'left':
            peer = value['peer'];await self.drop(peer);self.peers.pop(peer,None)
        elif kind == 'offer':
            peer = value['from']
            if peer >= self.name or peer not in self.peers:
                raise ValueError('Unexpected offer')
            await self.drop(peer)
            pc = self.connection(peer)
            async with asyncio.timeout(20):
                await pc.setRemoteDescription(RTCSessionDescription(value['description']['sdp'],'offer'))
                await pc.setLocalDescription(await pc.createAnswer())
                await self.socket.send_json({'type':'answer','to':peer,'description':{
                    'type':'answer','sdp':pc.localDescription.sdp}})
        elif kind == 'answer':
            peer = value['from']
            pc = self.links.get(peer)
            if pc and pc.signalingState == 'have-local-offer':
                await pc.setRemoteDescription(RTCSessionDescription(value['description']['sdp'],'answer'))

    async def heartbeat(self, stopped):
        while not stopped.is_set():
            if time.time() >= self.expires-30:
                await self.socket.close()
                return
            for peer, pc in list(self.links.items()):
                seen = self.observations.get(peer)
                if (pc.connectionState in ('failed','closed') or (seen and time.time()-seen['received']>45)
                        or (not seen and time.monotonic()-self.created[peer]>45)):
                    await self.drop(peer)
            for peer in list(self.peers):
                if self.name < peer and peer not in self.links:
                    await self.dial(peer)
            try:
                value = json.dumps(await self.status(),separators=(',',':'))
                for channel in list(self.channels.values()):
                    if channel.readyState=='open' and channel.bufferedAmount<65536:
                        channel.send(value)
            except Exception as error:
                logging.warning('Launcher status unavailable: %s',type(error).__name__)
            snapshot = {'name':self.name,'transport':'webrtc','signaling':'internet',
                        'peers':{peer:{'connection':self.links[peer].connectionState if peer in self.links else 'disconnected',
                                       **self.observations.get(peer,{})} for peer in self.peers if peer!=self.name}}
            path = Path('/tmp/launcher-peers-internet.json')
            temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(snapshot));temporary.replace(path)
            for _ in range(10):
                if stopped.is_set():
                    await self.socket.close();return
                await asyncio.sleep(1)

    async def run(self, stopped):
        from aiohttp import ClientSession, ClientTimeout, WSMsgType
        from aiortc import RTCConfiguration, RTCIceServer
        from launcher_registration import service_url
        base = service_url(self.registration['url'])
        while not stopped.is_set():
            ticker = None
            try:
                async with ClientSession(timeout=ClientTimeout(total=30),trust_env=False,headers={'User-Agent':'LifeTech-Launcher/1.0'}) as session:
                    async with session.post(base+'/launchers/session',headers={'Authorization':'Bearer '+self.registration['credential']},
                            json={'room':self.registration['room'],'peer':self.name},allow_redirects=False) as response:
                        if response.status in (401,403):
                            Path('/tmp/launcher-peers-internet.json').write_text(json.dumps({
                                'name':self.name,'registration':'new-invitation-required','peers':{}}))
                            logging.error('Launcher registration was revoked or expired; enroll with a new invitation')
                            return
                        if response.status!=200:raise ValueError('Device session rejected')
                        lease = await response.json()
                    self.expires = lease['expires'];token = lease['token']
                    async with session.get(base+'/ice',headers={'Authorization':'Bearer '+token},allow_redirects=False) as response:
                        if response.status!=200:raise ValueError('ICE configuration unavailable')
                        ice = await response.json()
                    self.rtc_config = RTCConfiguration(iceServers=[RTCIceServer(**server) for server in ice['iceServers']])
                    url = base.replace('https://','wss://',1)+'/signal'
                    async with session.ws_connect(url,protocols=['archive.v1','auth.'+token],heartbeat=20,max_msg_size=65536) as ws:
                        self.socket = ws
                        print('Launcher internet WebRTC signaling connected',flush=True)
                        ticker = asyncio.create_task(self.heartbeat(stopped))
                        def heartbeat_done(task):
                            if not task.cancelled() and task.exception() is not None:
                                logging.warning('Launcher heartbeat stopped: %s',type(task.exception()).__name__)
                                asyncio.create_task(ws.close())
                        ticker.add_done_callback(heartbeat_done)
                        async for message in ws:
                            if stopped.is_set():break
                            if message.type==WSMsgType.TEXT:
                                await self.signal(json.loads(message.data))
                            elif message.type in (WSMsgType.ERROR,WSMsgType.CLOSED):break
            except Exception as error:
                logging.warning('Launcher internet session unavailable: %s',type(error).__name__)
            finally:
                if ticker:
                    ticker.cancel()
                    await asyncio.gather(ticker,return_exceptions=True)
                await asyncio.gather(*(self.drop(peer) for peer in list(self.links)))
                self.peers = {self.name:self.name}
            for _ in range(10):
                if stopped.is_set():break
                await asyncio.sleep(1)


def main():
    import argparse
    import signal
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',default='lifetech-')
    args=parser.parse_args()
    path=Path(os.getenv('LIFETECH_PEER_CONFIG',str(CONFIG)))
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    asyncio.run(supervise(path,args.prefix,stopped))


LAUNCHER_PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LifeTech · Swarm</title>
<style>body{font:16px system-ui;background:#eff5f3;color:#173833;margin:0}main{max-width:900px;margin:auto;padding:32px 20px}h1{font-size:36px;margin:10px 0}h2{font-size:20px}section{background:white;padding:24px;border:1px solid #c6d9d2;border-radius:16px;margin:18px 0}label{display:block;margin:14px 0}button,select,input{font:inherit;padding:12px;border:1px solid #a4bdb5;border-radius:8px;max-width:100%;box-sizing:border-box}button{background:#17665b;color:white;cursor:pointer}input{width:100%}.muted{color:#526c65}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}#message{min-height:24px}small{display:block;margin-top:8px}</style>
<main><span class="muted">LIFETECH LAUNCHER</span><h1>Your place in the swarm</h1><p id="identity">Connecting to your launcher…</p>
<section><h2>Connections</h2><p id="connections"></p><button id="check">Check master connection</button> <button id="remote">Test WebRTC path</button><p id="connection-result" role="status"></p></section>
<section><h2>Contribute computing power</h2><label>Worker mode <select id="mode"><option value="auto">Automatic · AC power only</option><option value="always">Always accept jobs</option><option value="paused">Submit only · save battery</option></select></label><p id="power"></p><small class="muted">Already accepted jobs finish. You can submit work in every mode.</small></section>
<section><h2>Request tests</h2><form id="request"><label>Suite names, separated by commas<input id="suites" placeholder="monitor"></label><label>Or repository paths, separated by commas<input id="paths" placeholder="OrgPortal/web/src"></label><small class="muted">Requests publish the master's current code and run tests on available workers. They do not deploy.</small><p><button>Submit authenticated request</button></p></form><p id="message" role="status"></p><details><summary>Master results</summary><pre id="result"></pre></details></section></main>
<script>const $=id=>document.getElementById(id);async function api(path,body){const r=await fetch('/launcher/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);return d}async function refresh(){try{const d=await api('status');$('identity').textContent=d.node+' · master '+d.master;$('mode').value=d.worker.mode;$('power').textContent=d.worker.reason+(d.worker.batteries?.length?' · '+d.worker.batteries.map(b=>b.percent+'%').join(', '):'');const open=Object.values(d.webrtc_channels).filter(x=>x==='open').length;$('connections').textContent='LAN configured · '+(d.internet_enrolled?'Internet enrolled · '+open+' open WebRTC connection(s)':'Internet not enrolled');}catch(e){$('identity').textContent=e.message}}$('mode').onchange=async()=>{try{await api('policy',{mode:$('mode').value});await refresh()}catch(e){$('power').textContent=e.message}};async function check(transport){$('connection-result').textContent='Checking…';try{const d=await api('master',{transport});$('connection-result').textContent='Authenticated master responded via '+(transport==='webrtc'?'WebRTC':'LAN-first routing');$('result').textContent=JSON.stringify(d,null,2)}catch(e){$('connection-result').textContent=e.message}}$('check').onclick=()=>check('auto');$('remote').onclick=()=>check('webrtc');$('request').onsubmit=async e=>{e.preventDefault();const split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);const body={suites:split('suites'),paths:split('paths')};if(!body.suites.length&&!body.paths.length){$('message').textContent='Choose suites or paths to test.';return}const button=e.target.querySelector('button');button.disabled=true;try{const d=await api('request',body);$('message').textContent='Request accepted: '+d.id;$('result').textContent=JSON.stringify(d,null,2)}catch(e){$('message').textContent=e.message}finally{button.disabled=false}};refresh();setInterval(refresh,10000);</script></html>'''


class JobGateway:
    """Loopback gateway and bounded encrypted agent RPC over WebRTC data channels."""
    def __init__(self, agent):
        self.agent=agent;self.mesh=None;self.pending={};self.parts={};self.inflight=set();self.runner=None

    async def send(self, peer, ident, data, reply):
        import base64
        channel=self.mesh.channels.get(peer) if self.mesh else None
        if not channel or channel.readyState!='open':raise RuntimeError('Remote launcher is not connected')
        if len(data)>8*1024*1024:raise ValueError('RPC exceeds transport limit')
        async with asyncio.timeout(120):
            for index,offset in enumerate(range(0,len(data),12000)):
                while channel.bufferedAmount>262144:
                    if channel.readyState!='open':raise RuntimeError('Remote launcher disconnected')
                    await asyncio.sleep(.02)
                channel.send(json.dumps({'type':'agent-rpc','id':ident,'reply':reply,'seq':index,
                    'end':offset+12000>=len(data),'data':base64.b64encode(data[offset:offset+12000]).decode()}))

    def receive(self, peer, message):
        import base64,re
        config=json.loads(self.agent.config.read_text())
        if peer not in config.get('internet_nodes',{}).values():raise ValueError('Unenrolled RPC peer')
        ident=message['id'];reply=message['reply']
        if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident) or type(reply)!=bool:raise ValueError('Invalid RPC frame')
        key=(peer,ident,reply)
        if reply and (peer,ident) not in self.pending:raise ValueError('Unsolicited RPC response')
        self.parts={k:v for k,v in self.parts.items() if v['time']>time.monotonic()-120}
        if key not in self.parts:
            if message['seq']!=0 or len(self.parts)>=8:raise ValueError('RPC assembly limit')
            self.parts[key]={'data':bytearray(),'seq':0,'time':time.monotonic()}
        state=self.parts[key]
        if message['seq']!=state['seq'] or len(message['data'])>16000:raise ValueError('Invalid RPC sequence')
        state['data'].extend(base64.b64decode(message['data'],validate=True));state['seq']+=1
        if len(state['data'])>8*1024*1024:
            del self.parts[key];raise ValueError('RPC assembly limit')
        if message.get('end') is not True:return
        data=bytes(self.parts.pop(key)['data'])
        if reply:
            future=self.pending[(peer,ident)]
            if not future.done():future.set_result(data)
        else:
            if len(self.inflight)>=8:raise ValueError('RPC concurrency limit')
            async def respond():
                try:await self.send(peer,ident,await self.agent.process(data),True)
                except Exception as error:logging.warning('Remote agent request failed: %s',type(error).__name__)
            task=asyncio.create_task(respond());self.inflight.add(task);task.add_done_callback(self.inflight.discard)

    async def remote(self, node, raw):
        config=json.loads(self.agent.config.read_text());peer=config.get('internet_nodes',{}).get(node)
        if not peer:raise RuntimeError('No internet enrollment for this node')
        if len(self.pending)>=8:raise RuntimeError('Remote request limit')
        ident=secrets.token_hex(16);future=asyncio.get_running_loop().create_future();self.pending[(peer,ident)]=future
        try:
            await self.send(peer,ident,raw,False)
            return await asyncio.wait_for(future,180)
        finally:self.pending.pop((peer,ident),None)

    async def request(self, request):
        from aiohttp import web,ClientSession,ClientTimeout,ClientConnectorError,ConnectionTimeoutError
        # This bridge never decrypts caller traffic, and only contacts enrolled peers.
        if request.headers.get('Origin'):raise web.HTTPForbidden()
        node=request.headers.get('X-Lifetech-Peer');config=json.loads(self.agent.config.read_text())
        if node not in config['peers']:raise web.HTTPForbidden()
        raw=await request.read()
        try:
            if request.headers.get('X-Lifetech-Transport')!='webrtc':
                try:
                    async with ClientSession(timeout=ClientTimeout(total=180,connect=3),trust_env=False) as session:
                        async with session.post('http://'+config['peers'][node]+':8790/agent/v1',data=raw,allow_redirects=False) as response:
                            if response.status!=200:raise RuntimeError('Agent rejected request')
                            data=bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                data.extend(chunk)
                                if len(data)>8*1024*1024:raise ValueError('RPC response too large')
                            data=bytes(data)
                            if len(data)>8*1024*1024:raise ValueError('RPC response too large')
                            return web.Response(body=data)
                except (ClientConnectorError,ConnectionTimeoutError):pass
            return web.Response(body=await self.remote(node,raw))
        except Exception as error:raise web.HTTPBadGateway(text=type(error).__name__+': '+str(error)[:200])

    def local_browser(self, request):
        from aiohttp import web
        origin=request.headers.get('Origin')
        if request.host not in ('127.0.0.1:8791','localhost:8791') or (origin and origin not in ('http://127.0.0.1:8791','http://localhost:8791')):
            raise web.HTTPForbidden()

    async def master_call(self, op, args, transport='auto'):
        from aiohttp import ClientSession,ClientTimeout
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        config=json.loads(self.agent.config.read_text());node=config.get('master','compai.local')
        cipher=AESGCM(bytes.fromhex(config['key']));ident=secrets.token_hex(16);nonce=secrets.token_bytes(12)
        value={'from':config['name'],'to':node,'time':time.time(),'nonce':ident,'op':op,'args':args}
        raw=nonce+cipher.encrypt(nonce,json.dumps(value).encode(),b'lifetech-agent.v1')
        async with ClientSession(timeout=ClientTimeout(total=190),trust_env=False) as session:
            async with session.post('http://127.0.0.1:8791/agent/v1',data=raw,
                    headers={'X-Lifetech-Peer':node,'X-Lifetech-Transport':transport}) as response:
                if response.status!=200:raise RuntimeError((await response.text())[:300])
                raw=await response.read()
        value=json.loads(cipher.decrypt(raw[:12],raw[12:],b'lifetech-agent.v1'))
        if value.get('nonce')!=ident or value.get('node')!=node or abs(time.time()-value['time'])>60:raise ValueError('Master response identity mismatch')
        if not value.get('ok'):raise RuntimeError(value.get('error','Master request failed'))
        return value['result']

    async def browser(self, request):
        from aiohttp import web
        from launcher_agent import worker_policy
        self.local_browser(request)
        if request.path=='/launcher':
            return web.Response(text=LAUNCHER_PAGE,content_type='text/html',headers={'Cache-Control':'no-store','X-Frame-Options':'DENY'})
        try:
            if request.method=='GET':
                config=json.loads(self.agent.config.read_text())
                peers={}
                if self.mesh:peers={p:c.readyState for p,c in self.mesh.channels.items()}
                return web.json_response({'node':config['name'],'worker':worker_policy(self.agent.config),
                    'lan':config.get('peers',{}),'internet_enrolled':bool(config.get('internet')),'webrtc_channels':peers,
                    'master':config.get('master','compai.local')},headers={'Cache-Control':'no-store'})
            body=await request.json()
            if request.path=='/launcher/policy':
                if body.get('mode') not in ('auto','always','paused'):raise ValueError('Invalid worker mode')
                path=self.agent.config.parent/'worker-policy.json';temp=path.with_suffix('.tmp')
                temp.write_text(json.dumps({'mode':body['mode']}));temp.chmod(0o600);temp.replace(path)
                return web.json_response(worker_policy(self.agent.config))
            if request.path=='/launcher/request':
                result=await self.master_call('tests.request',body)
            else:
                result=await self.master_call('tests.status',{},body.get('transport','auto'))
            return web.json_response(result)
        except Exception as error:return web.json_response({'error':str(error)[:500]},status=409)

    async def start(self):
        from aiohttp import web
        app=web.Application(client_max_size=2*1024*1024)
        app.router.add_post('/agent/v1',self.request)
        app.router.add_get('/launcher',self.browser)
        app.router.add_get('/launcher/status',self.browser)
        for route in ('policy','request','master'):app.router.add_post('/launcher/'+route,self.browser)
        self.runner=web.AppRunner(app,access_log=None);await self.runner.setup()
        await web.TCPSite(self.runner,'127.0.0.1',8791).start()


class EnrollmentPoller:
    """Retry only provisioned invitations; never auto-join without fleet authorization."""
    def __init__(self, path):
        self.path = Path(path)
        self.invitation = self.path.parent/'invitation.json'
        self.fingerprint = None
        self.next_attempt = 0
        self.delay = 5
        self.blocked = False
        self.last_state = None

    def state(self, value):
        if value != self.last_state:
            logging.info('Launcher enrollment: %s', value)
            self.last_state = value
        target = Path('/tmp/launcher-enrollment.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps({'status':value,'updated':time.time()}))
        temporary.replace(target)

    async def poll(self, config):
        import base64
        import hashlib
        import urllib.error
        from launcher_registration import register
        internet = config.get('internet',{})
        registration_state = 'registered'
        try:
            observed = json.loads(Path('/tmp/launcher-peers-internet.json').read_text())
            if observed.get('name')==internet.get('peer') and observed.get('registration')=='new-invitation-required':
                registration_state = 'new-invitation-required'
        except (OSError,ValueError):pass
        if not self.invitation.is_file():
            self.state(registration_state if internet and not internet.get('pending') else 'waiting-for-invitation')
            return config
        try:
            raw = self.invitation.read_bytes()
            if len(raw)>16384:raise ValueError('Invitation too large')
            fingerprint = hashlib.sha256(raw).hexdigest()
            if fingerprint != self.fingerprint:
                self.fingerprint = fingerprint
                self.next_attempt = 0
                self.delay = 5
                self.blocked = False
            value = json.loads(raw)
            if 'invitations' in value:
                if len(value['invitations'])!=1:raise ValueError('One device invitation required')
                value = value['invitations'][0]
            claims = json.loads(base64.urlsafe_b64decode(value['token'].split('.')[1]+'==='))
            if (internet.get('peer')==claims['peer'] and internet.get('room')==claims['room']
                    and not internet.get('pending')):
                # A revoked credential requires a different invitation, not repeated enrollment.
                self.state(registration_state)
                return config
            if claims.get('aud')!='launcher-enroll-v1' or claims['exp']<=time.time():
                self.blocked = True
                self.state('new-invitation-required')
                return config
            if self.blocked or time.monotonic()<self.next_attempt:
                return config
            self.state('registering')
            await asyncio.to_thread(register,self.invitation,self.path)
            config = json.loads(self.path.read_text())
            self.state('registered')
            self.delay = 5
            return config
        except urllib.error.HTTPError as error:
            if error.code in (400,401,403,409,410,422):
                self.blocked = True
                self.state('new-invitation-required')
            else:
                self.retry()
        except (OSError,TimeoutError):
            self.retry()
        except (ValueError,KeyError,TypeError,IndexError):
            self.blocked = True
            self.state('invalid-invitation')
        return config

    def retry(self):
        self.next_attempt = time.monotonic()+self.delay+secrets.randbelow(5)
        self.delay = min(self.delay*2,300)
        self.state('registration-retry-pending')


async def supervise(path,prefix,stopped):
    """Watch enrollment and run LAN/internet sessions independently without restart."""
    path = Path(path)
    poller = EnrollmentPoller(path)
    from launcher_agent import Agent
    agent=Agent(path,os.environ.get('LIFETECH_JOB_ROOT',str(path.parent/'jobs')))
    gateway=JobGateway(agent)
    await gateway.start()
    tasks = {}
    try:
        while not stopped.is_set():
            try:
                config = json.loads(path.read_text()) if path.is_file() else {}
                if not isinstance(config,dict):raise ValueError('Invalid peer configuration')
                config = await poller.poll(config)
                desired = {}
                if all(key in config for key in ('name','peers','key')):
                    desired['lan'] = {key:config[key] for key in ('name','peers','key')}
                if config.get('internet') and not config['internet'].get('pending'):
                    desired['internet'] = {'internet':config['internet'],'internet_nodes':config.get('internet_nodes',{})}
                for name,(version,task) in list(tasks.items()):
                    changed = name not in desired or version!=json.dumps(desired[name],sort_keys=True)
                    if changed:
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                        del tasks[name]
                for name,value in desired.items():
                    if name in tasks:
                        continue
                    mesh = Mesh(value,prefix,agent) if name=='lan' else InternetMesh(value,prefix)
                    if name=='internet':mesh.gateway=gateway;gateway.mesh=mesh
                    task = asyncio.create_task(mesh.run(stopped),name='launcher-'+name)
                    tasks[name] = (json.dumps(value,sort_keys=True),task)
                    def completed(done):
                        if not done.cancelled() and done.exception() is not None:
                            logging.error('Launcher peer session stopped: %s',type(done.exception()).__name__)
                    task.add_done_callback(completed)
                # A completed revoked internet session stays stopped until credentials change.
                # LAN startup failures retry, since a temporarily occupied port may become free.
                if 'lan' in tasks and tasks['lan'][1].done():
                    del tasks['lan']
            except (OSError,ValueError,KeyError,TypeError):
                poller.state('waiting-for-valid-configuration')
            for _ in range(5):
                if stopped.is_set():break
                await asyncio.sleep(1)
    finally:
        await gateway.runner.cleanup()
        for task in gateway.inflight:task.cancel()
        await asyncio.gather(*gateway.inflight,return_exceptions=True)
        await asyncio.to_thread(agent.close)
        for _,task in tasks.values():task.cancel()
        await asyncio.gather(*(task for _,task in tasks.values()),return_exceptions=True)


if __name__=='__main__':main()
