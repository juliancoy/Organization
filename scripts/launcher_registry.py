"""Loopback Docker registry bridge over authenticated agent requests."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
import urllib.request
import urllib.error

PROTOCOL='lifetech-agent.v1'
CHUNK=4*1024*1024
PATH=re.compile(r'/v2/tests/[a-f0-9]{24}/(?:manifests|blobs)/sha256:[a-f0-9]{64}')


def read(args):
    path=args['path'];offset=args.get('offset',0)
    if not PATH.fullmatch(path):raise ValueError('Invalid registry path')
    if not isinstance(offset,int) or offset<0:raise ValueError('Invalid registry range')
    head=args.get('head') is True
    headers={'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'}
    if not head:headers['Range']=f'bytes={offset}-{offset+CHUNK-1}'
    request=urllib.request.Request('http://127.0.0.1:18791'+path,headers=headers,method='HEAD' if head else 'GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=30) as response:
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
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    cipher=AESGCM(bytes.fromhex(config['key']))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request_chunk(path,offset,head=False):
        request_id=secrets.token_hex(16)
        payload={'from':config['name'],'to':coordinator,'time':time.time(),'nonce':request_id,
                 'op':'registry.read','args':{'path':path,'offset':offset,'head':head}}
        nonce=secrets.token_bytes(12)
        request=urllib.request.Request('http://'+config['peers'][coordinator]+':8790/agent/v1',
            data=nonce+cipher.encrypt(nonce,json.dumps(payload).encode(),PROTOCOL.encode()),
            headers={'Content-Type':'application/octet-stream'})
        with opener.open(request,timeout=60) as response:raw=response.read(2*CHUNK)
        value=json.loads(cipher.decrypt(raw[:12],raw[12:],PROTOCOL.encode()))
        if value.get('nonce')!=request_id or value.get('node')!=coordinator or abs(time.time()-value['time'])>60:raise ValueError('Invalid registry response identity or freshness')
        if not value.get('ok'):raise RuntimeError(value.get('error','Registry read failed'))
        return value['result']
    errors=[]
    layers={}
    started=time.monotonic()
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
            if self.path=='/v2/':
                self.send_response(200);self.send_header('Content-Length','2');self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers()
                if body:self.wfile.write(b'{}')
                return
            if not PATH.fullmatch(self.path) or not self.path.startswith('/v2/'+repository+'/'):
                self.send_error(404);return
            sent=False
            try:
                requested=self.headers.get('Range')
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested) if requested else None
                if requested and not match:raise ValueError('Invalid registry byte range')
                offset=int(match[1]) if match else 0
                first=request_chunk(self.path,offset,head=not body);total=first['total']
                end=min(total-1,int(match[2])) if match and match[2] else total-1
                if offset>end:raise ValueError('Invalid registry byte range bounds')
                self.send_response(206 if match else 200);self.send_header('Content-Type',first['type']);self.send_header('Content-Length',str(end-offset+1))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Docker-Content-Digest',self.path.rsplit('/',1)[-1])
                if match:self.send_header('Content-Range',f'bytes {offset}-{end}/{total}')
                self.send_header('Docker-Distribution-Api-Version','registry/2.0');self.end_headers();sent=True
                if not body:return
                value=first
                while offset<=end:
                    data=base64.b64decode(value['data'],validate=True)[:end-offset+1]
                    if not data or len(data)>CHUNK or offset+len(data)>total or value['total']!=total:raise ValueError('Invalid registry chunk')
                    self.wfile.write(data);offset+=len(data)
                    if offset<=end:value=request_chunk(self.path,offset)
            except Exception as error:
                errors.append(type(error).__name__+': '+str(error)[:400])
                self.close_connection=True
                if not sent:self.send_error(502,'Authenticated registry bridge failed')
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
                elapsed=time.monotonic()-started
                speed=current/elapsed if elapsed>=3 else 0
                remaining=max(0,total-current)
                with agent.lock:
                    agent.image_progress[args['image']]={'transfer_phase':'downloading' if remaining else 'finalizing',
                        'bytes_completed':current,'bytes_total':total,'bytes_per_second':speed,
                        'eta_seconds':remaining/speed if speed>0 and remaining else None,
                        'updated':time.time()}
            image=client.images.get(reference)
            if agent.image_fingerprint(image)!=args['fingerprint']:raise ValueError('Registry image contents differ from coordinator')
            image.tag(args['image'])
            with agent.lock:agent.image_progress.pop(args['image'],None)
            return {'id':image.id}
    except Exception as error:
        detail=('; registry bridge: '+errors[-1]) if errors else ''
        raise RuntimeError(str(error)[:1000]+detail) from error
    finally:
        with agent.lock:agent.image_progress.pop(args['image'],None)
        server.shutdown();server.server_close();thread.join(timeout=5)

