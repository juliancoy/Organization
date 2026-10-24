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
    def __init__(self, config, prefix):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.name, self.peers = config['name'], config['peers']
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
                host = self.peers[peer]
                url = f'http://{host}:{PORT}/offer'
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
        app = web.Application(client_max_size=65536)
        app.router.add_post('/offer', self.offer)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', PORT).start()
        print(f'Launcher WebRTC listening on LAN port {PORT}; {len(self.peers)-1} enrolled peers', flush=True)
        initial = {'name':self.name,'peers':{peer:{'connection':'disconnected'} for peer in self.peers if peer!=self.name}}
        path = Path('/tmp/launcher-peers.json')
        temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(initial));temporary.replace(path)
        try:
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
                    temporary.write_text(json.dumps({'name': self.name, 'peers': snapshot}))
                    temporary.replace(path)
                    for _ in range(10):
                        if stopped.is_set():
                            break
                        await asyncio.sleep(1)
        finally:
            await self.runner.cleanup()
            await asyncio.gather(*(self.drop(peer) for peer in list(self.links)))


def start(prefix):
    path = Path(os.getenv('LIFETECH_PEER_CONFIG', str(CONFIG)))
    stopped = threading.Event()

    def serve():
        try:
            asyncio.run(supervise(path,prefix,stopped))
        except Exception:
            logging.exception('Launcher WebRTC stopped')
    thread = threading.Thread(target=serve, name='launcher-webrtc', daemon=True)
    thread.start()
    return stopped.set


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
                async with ClientSession(timeout=ClientTimeout(total=30),trust_env=False) as session:
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
        if not self.invitation.is_file():
            self.state('registered' if internet and not internet.get('pending') else 'waiting-for-invitation')
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
                self.state('registered')
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
                    desired['internet'] = {'internet':config['internet']}
                for name,(version,task) in list(tasks.items()):
                    changed = name not in desired or version!=json.dumps(desired[name],sort_keys=True)
                    if changed:
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                        del tasks[name]
                for name,value in desired.items():
                    if name in tasks:
                        continue
                    mesh = Mesh(value,prefix) if name=='lan' else InternetMesh(value,prefix)
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
        for _,task in tasks.values():task.cancel()
        await asyncio.gather(*(task for _,task in tasks.values()),return_exceptions=True)


if __name__=='__main__':main()
