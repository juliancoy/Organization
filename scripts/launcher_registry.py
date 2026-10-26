"""Loopback Docker registry bridge over authenticated agent requests."""
import base64
import errno
import hashlib
from pathlib import Path
import os
import socket
import shutil
import tempfile
from collections import deque
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

PROTOCOL='lifetech-agent.v1'
CHUNK=4*1024*1024
CACHE_LOCK=threading.Lock()
PATH=re.compile(r'/v2/tests/[a-f0-9]{24}/(?:manifests|blobs)/sha256:[a-f0-9]{64}')


class MultipathSocket(socket.socket):
    @property
    def proto(self):
        # asyncio resolves even numeric addresses with sock.proto. libc's
        # getaddrinfo rejects protocol 262; DNS resolution still uses TCP.
        # SO_PROTOCOL on the actual descriptor remains IPPROTO_MPTCP.
        return socket.IPPROTO_TCP


def transport_socket(address_info):
    """Negotiate MPTCP when supported; kernel transparently accepts TCP peers."""
    family,kind,protocol,*_=address_info
    if family in (socket.AF_INET,socket.AF_INET6):
        try:return MultipathSocket(family,kind,getattr(socket,'IPPROTO_MPTCP',262))
        except OSError as error:
            if error.errno not in (errno.EPROTONOSUPPORT,errno.ENOPROTOOPT,errno.EINVAL,errno.EAFNOSUPPORT,errno.EPERM):raise
    return socket.socket(family,kind,protocol)


def cache_read(cache,path,offset,head):
    if cache is None:return None
    digest=path.rsplit(':',1)[1]
    file=Path(cache)/digest
    try:
        with file.open('rb') as stream:
            total=os.fstat(stream.fileno()).st_size
            stream.seek(offset);data=b'' if head else stream.read(CHUNK)
        media=(Path(cache)/(digest+'.type')).read_text()
        if len(media)>256 or '\r' in media or '\n' in media:return None
        return {'data':base64.b64encode(data).decode(),'total':total,'type':media}
    except OSError:return None


def cache_publish(cache,temporary,path,media,total):
    with CACHE_LOCK:return _cache_publish(cache,temporary,path,media,total)


def _cache_publish(cache,temporary,path,media,total):
    """Only complete digest-verified objects become peer-readable."""
    if not isinstance(media,str) or len(media)>256 or '\r' in media or '\n' in media:raise ValueError('Invalid cached media type')
    temporary.flush();temporary.seek(0)
    if total>2*1024**3:return False
    if os.fstat(temporary.fileno()).st_size!=total:return False
    digest=path.rsplit(':',1)[1]
    if hashlib.file_digest(temporary,'sha256').hexdigest()!=digest:raise ValueError('Layer cache digest mismatch')
    cache=Path(cache);destination=cache/digest
    (cache/(digest+'.type')).write_text(media)
    os.replace(temporary.name,destination)
    # Bound retained compressed layers and manifests to 2 GiB, oldest first.
    files=sorted((p for p in cache.iterdir() if re.fullmatch('[a-f0-9]{64}',p.name)),key=lambda p:p.stat().st_mtime)
    size=sum(p.stat().st_size for p in files)
    for file in files:
        if size<=2*1024**3:break
        size-=file.stat().st_size;file.unlink(missing_ok=True);file.with_name(file.name+'.type').unlink(missing_ok=True)
    return True


def read(args,cache=None):
    path=args['path'];offset=args.get('offset',0)
    if not PATH.fullmatch(path):raise ValueError('Invalid registry path')
    if not isinstance(offset,int) or offset<0:raise ValueError('Invalid registry range')
    head=args.get('head') is True
    cached=cache_read(cache,path,offset,head)
    if cached is not None:return cached
    headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'}
    if not head:headers['Range']=f'bytes={offset}-{offset+CHUNK-1}'
    request=urllib.request.Request('http://127.0.0.1:18791'+path,headers=headers,method='HEAD' if head else 'GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        response=opener.open(request,timeout=30)
    except urllib.error.URLError as error:
        if not isinstance(error,urllib.error.HTTPError):return {'status':404,'data':'','total':0,'type':'application/json'}

        with error:
            detail=error.read(4096).decode(errors='replace')
            if error.code==404:
                return {'status':404,'data':'','total':0,'type':'application/json'}
            raise RuntimeError(f'Registry {head and "HEAD" or "GET"} {path}: HTTP {error.code}: {detail}') from error
    with response:
        if offset and response.status!=206:raise RuntimeError('Registry did not honor the blob range')
        total=int(response.headers.get('Content-Range','/').rsplit('/',1)[-1]) if response.status==206 else int(response.headers['Content-Length'])
        data=b'' if head else response.read(CHUNK)
        return {'data':base64.b64encode(data).decode(),'total':total,'type':response.headers.get('Content-Type','application/octet-stream')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RuntimeError('Registry redirects are forbidden')


def pull(agent,args):
    repository=args['repository'];digest=args['digest'];coordinator=args['coordinator']
    if not re.fullmatch('tests/[a-f0-9]{24}',repository) or not re.fullmatch('sha256:[a-f0-9]{64}',digest):raise ValueError('Invalid immutable registry reference')
    config=json.loads(agent.config.read_text())
    if coordinator not in config['peers']:raise ValueError('Registry coordinator is not enrolled')
    sources=list(dict.fromkeys(args.get('sources',[])+[coordinator]))
    if len(sources)>24 or any(node not in config['peers'] or node==config['name'] for node in sources):raise ValueError('Invalid enrolled image sources')
    cache=agent.config.parent/'layer-cache';cache.mkdir(mode=0o700,exist_ok=True)
    selected={};source_bytes={}
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    cipher=AESGCM(bytes.fromhex(config['key']))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request_chunk(path,offset,head=False):
        last=None
        for node in dict.fromkeys(([selected[path]] if path in selected else [])+sources):
            try:
                request_id=secrets.token_hex(16)
                payload={'from':config['name'],'to':node,'time':time.time(),'nonce':request_id,
                         'op':'registry.read','args':{'path':path,'offset':offset,'head':head}}
                nonce=secrets.token_bytes(12)
                request=urllib.request.Request('http://127.0.0.1:8791/agent/v1',
                    data=nonce+cipher.encrypt(nonce,json.dumps(payload).encode(),PROTOCOL.encode()),
                    headers={'Content-Type':'application/octet-stream','X-Lifetech-Peer':node})
                with opener.open(request,timeout=60) as response:raw=response.read(2*CHUNK)
                value=json.loads(cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
                if value.get('nonce')!=request_id or value.get('node')!=node or abs(time.time()-value['time'])>60:raise ValueError('Invalid registry response identity or freshness')
                if not value.get('ok'):raise RuntimeError(value.get('error','Registry read failed'))
                result=value['result']
                if result.get('status')==404:continue
                selected[path]=node
                source_bytes[node]=source_bytes.get(node,0)+len(base64.b64decode(result.get('data',''),validate=True))
                return result
            except (OSError,ValueError,RuntimeError) as error:last=error
        if last:raise RuntimeError('No enrolled image source available') from last
        return {'status':404,'data':'','total':0,'type':'application/json'}
    errors=deque(maxlen=10)
    layers={}
    started=time.monotonic()
    samples=deque([(started,0)])
    with agent.lock:agent.image_progress[args['image']]={'transfer_phase':'starting','updated':time.time()}

    class Proxy(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*_):pass
        def handle(self):
            # Docker probes HTTPS first, then permits HTTP for loopback registries.
            # Return an HTTP status line for a TLS probe so Go recognizes that fallback.
            if self.rfile.peek(1)[:1]==b'\x16':
                self.connection.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                self.close_connection=True
                return
            super().handle()
        def do_HEAD(self):self.serve(False)
        def do_GET(self):self.serve(True)
        def serve(self,body):
            # Docker may append a registry namespace query to immutable requests.
            path=urlsplit(self.path).path
            if path=='/v2/':
                self.send_response(200);self.send_header('Content-Length','2');self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers()
                if body:self.wfile.write(b'{}')
                return
            if not PATH.fullmatch(path) or not path.startswith('/v2/'+repository+'/'):
                self.send_error(404);return
            sent=False;cached=None
            try:
                requested=self.headers.get('Range')
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested) if requested else None
                if requested and not match:raise ValueError('Invalid registry byte range')
                offset=int(match[1]) if match else 0
                first=request_chunk(path,offset,head=not body)
                if first.get('status')==404:
                    errors.append('Registry resource missing: '+path)
                    self.send_error(404,'Registry resource missing');return
                total=first['total']
                end=min(total-1,int(match[2])) if match and match[2] else total-1
                if offset>end:raise ValueError('Invalid registry byte range bounds')
                self.send_response(206 if match else 200);self.send_header('Content-Type',first['type']);self.send_header('Content-Length',str(end-offset+1))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Docker-Content-Digest',path.rsplit('/',1)[-1])
                if match:self.send_header('Content-Range',f'bytes {offset}-{end}/{total}')
                self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers();sent=True
                if not body:return
                if offset==0 and end==total-1 and total<=2*1024**3 and shutil.disk_usage(cache).free>total+128*1024**2:cached=tempfile.NamedTemporaryFile(mode='w+b',dir=cache,prefix='.partial-',delete=False)
                value=first
                while offset<=end:
                    data=base64.b64decode(value['data'],validate=True)[:end-offset+1]
                    if not data or len(data)>CHUNK or offset+len(data)>total or value['total']!=total:raise ValueError('Invalid registry chunk')
                    if cached:cached.write(data)
                    self.wfile.write(data);offset+=len(data)
                    if offset<=end:value=request_chunk(path,offset)
                if cached:cache_publish(cache,cached,path,first['type'],total)
            except Exception as error:
                errors.append(type(error).__name__+': '+str(error)[:400])
                self.close_connection=True
                if not sent:self.send_error(502,'Authenticated registry bridge failed')
            finally:
                if cached:cached.close();Path(cached.name).unlink(missing_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),Proxy);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    reference=f'127.0.0.1:{server.server_port}/{repository}@{digest}'
    try:
        with agent.client() as client:
            for event in client.api.pull(reference,stream=True,decode=True):
                if event.get('error'):raise RuntimeError(event['error'])
                detail=event.get('progressDetail') or {}
                layer=event.get('id')
                if layer and event.get('status')=='Downloading' and detail.get('total'):
                    layers[layer]=(int(detail.get('current',0)),int(detail['total']))
                elif layer in layers and event.get('status') in ('Download complete','Pull complete'):
                    layers[layer]=(layers[layer][1],layers[layer][1])
                current=sum(row[0] for row in layers.values())
                total=sum(row[1] for row in layers.values())
                now=time.monotonic()
                samples.append((now,current))
                while len(samples)>2 and samples[1][0]<now-15:
                    samples.popleft()
                elapsed=now-samples[0][0]
                speed=max(0,current-samples[0][1])/elapsed if elapsed>=3 else 0
                remaining=max(0,total-current)
                with agent.lock:
                    agent.image_progress[args['image']]={'transfer_phase':'downloading' if remaining else 'finalizing',
                        'bytes_completed':current,'bytes_total':total,'bytes_per_second':speed,
                        'eta_seconds':remaining/speed if speed>0 and remaining else None,
                        'sources':dict(source_bytes),'updated':time.time()}
            image=client.images.get(reference)
            if agent.image_fingerprint(image)!=args['fingerprint']:raise ValueError('Registry image contents differ from coordinator')
            image.tag(args['image'])
            with agent.lock:agent.image_progress.pop(args['image'],None)
            return {'id':image.id,'source_bytes':dict(source_bytes)}
    except Exception as error:
        detail=('Registry bridge: '+errors[-1]+'; ') if errors else ''
        raise RuntimeError((detail+str(error))[:1000]) from error
    finally:
        with agent.lock:agent.image_progress.pop(args['image'],None)
        server.shutdown();server.server_close();thread.join(timeout=5)

