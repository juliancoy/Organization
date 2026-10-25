"""Narrow owner-authenticated Codex web service for an HTTPS tunnel.

Bind only to loopback. The tunnel publishes this service, never the test master.
A fixed test APK can be delivered; no shell, proxy, or arbitrary-file endpoints are exposed.
"""
import json
import logging
from http.cookies import SimpleCookie
import codex_devices
import mimetypes
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import codex_context
from master_api import authorized

VIEW=Path(__file__).with_name('view')
ASSETS={'/':'codex.html','/codex.html':'codex.html','/codex.js':'codex.js','/codex.css':'codex.css','/style.css':'style.css','/test-deployment.js':'test-deployment.js',
        '/test-deployment.css':'test-deployment.css','/swarm-control':'swarm-control.html','/swarm-control.svg':'swarm-control.svg'}
ASSETS.update({'/swarm-status':'swarm-status.html','/swarm-status.js':'swarm-status.js','/swarm-status.css':'swarm-status.css','/swarm-machines.js':'swarm-machines.js'})
API='/api/v1/master/codex/'

def audit_auth_failure(error,phase):
    reasons={
        'Invalid PIdP callback.':'invalid_callback_parameters',
        'PIdP callback issuer does not match.':'issuer_mismatch',
        'Restart sign-in in this browser.':'missing_browser_cookie',
        'Sign-in expired or belongs to another browser.':'expired_or_wrong_browser',
        'This PIdP account is not the CompAI owner.':'owner_mismatch',
        'PIdP token is for a different resource.':'token_binding_mismatch',
        'PIdP token expired or is not yet valid.':'token_lifetime_invalid',
    }
    # Fixed diagnostic labels only; never exception text, URLs, identities or credentials.
    reason=reasons.get(str(error),'authentication_error')
    status=getattr(error,'code',None)
    logging.warning('swarm_auth_failed phase=%s reason=%s exception=%s http_status=%s',
                    phase,reason,type(error).__name__,status if type(status) is int else 'none')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass  # Never retain credentials, URLs, or conversation text.
    def device(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''));return cookie['__Host-compai_device'].value if '__Host-compai_device' in cookie else None
        except Exception:return None
    def reply(self,status,value,kind='application/json',cookie=None,location=None):
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.send_response(status)
        for k,v in {'Content-Type':kind,'Content-Length':str(len(raw)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",'Strict-Transport-Security':'max-age=31536000'}.items():self.send_header(k,v)
        if kind=='application/vnd.android.package-archive':self.send_header('Content-Disposition','attachment; filename="swarm-control-test.apk"')
        if cookie is not None:
            for item in cookie if isinstance(cookie,list) else [cookie]:self.send_header('Set-Cookie',item)
        if location is not None:self.send_header('Location',location)
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def handle_request(self):
        origin=self.server.public_origin
        if len(self.headers.get_all('Host',[]))!=1 or self.headers['Host']!=urlsplit(origin).netloc:
            return self.reply(403,{'error':{'message':'Host denied'}})
        if self.headers.get('Origin') not in (None,origin):return self.reply(403,{'error':{'message':'Origin denied'}})
        path=urlsplit(self.path).path
        if self.command=='GET' and path in ('/auth/login','/auth/callback'):
            try:
                import codex_pidp
                if path=='/auth/login':
                    target,cookie=codex_pidp.start(origin)
                    return self.reply(303,{},cookie=cookie,location=target)
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                browser=cookies[codex_pidp.LOGIN_COOKIE].value if codex_pidp.LOGIN_COOKIE in cookies else None
                token=codex_pidp.complete(origin,parse_qs(urlsplit(self.path).query,keep_blank_values=True),browser)
                return self.reply(303,{},cookie=[
                    '__Host-compai_device='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age='+str(codex_devices.LIFETIME),
                    codex_pidp.LOGIN_COOKIE+'=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0'],location='/')
            except Exception as error:
                audit_auth_failure(error,'login' if path=='/auth/login' else 'callback')
                return self.reply(403,{'error':{'message':'PIdP sign-in failed. Use the CompAI owner account and restart at /auth/login.'}})
        if self.command=='GET' and path in ASSETS:
            name=ASSETS[path];return self.reply(200,(VIEW/name).read_bytes(),mimetypes.guess_type(name)[0] or 'application/octet-stream')
        pairing=self.command=='POST' and path in ('/pair','/logout')
        deployment=path in ('/api/v1/test-app/status','/api/v1/test-app/offer','/api/v1/test-app/download')
        valid_read=self.command=='GET' and (path=='/api/v1/swarm/status' or path==API+'threads' or path.startswith(API+'threads/') or path in ('/api/v1/test-app/status','/api/v1/test-app/download'))
        valid_write=self.command=='POST' and path in (API+'commands','/api/v1/test-app/offer')
        if not (valid_read or valid_write or pairing):return self.reply(404,{'error':{'message':'Not found'}})
        if path not in ('/pair','/logout') and not codex_devices.valid(self.device()):return self.reply(401,{'error':{'message':'Sign in with PIdP to reconnect to CompAI'}})
        body=None
        try:
            if valid_write or pairing:
                if self.headers.get('Origin')!=origin:return self.reply(403,{'error':{'message':'Same-origin browser required'}})
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError()
                size=int(self.headers.get('Content-Length','-1'))
                if not 0<size<=65536:raise ValueError()
                self.connection.settimeout(10)
                raw=self.rfile.read(size)
                if len(raw)!=size:raise ValueError()
                body=json.loads(raw)
            if pairing:
                if path=='/logout':
                    if self.device():codex_devices.revoke(self.device())
                    return self.reply(200,{'ok':True},cookie='__Host-compai_device=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')
                if path=='/pair' and (not isinstance(body,dict) or set(body)!={'token'}):raise ValueError()
                token=codex_devices.session(body['token'])
                return self.reply(200,{'ok':True},cookie='__Host-compai_device='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age='+str(codex_devices.LIFETIME))
            if path=='/api/v1/swarm/status':
                from swarm_status import snapshot
                try:return self.reply(200,snapshot())
                except (OSError,ValueError):return self.reply(503,{'error':{'message':'Local swarm coordinator unavailable'}})
            if deployment:
                from swarm_test_deployment import manager,artifact
                if path.endswith('/download'):
                    data,_=artifact()
                    return self.reply(200,data,'application/vnd.android.package-archive')
                if path.endswith('/status'):return self.reply(200,manager().status())
                device=self.device()
                valid=lambda: codex_devices.valid(device)
                return self.reply(200,manager().offer(body,valid))
            code,value=codex_context.rest(self.command,path,parse_qs(urlsplit(self.path).query),body)
            self.reply(code,value)
        except ImportError:return self.reply(503,{'error':{'message':'P2P deployment dependencies are not installed on CompAI'}})
        except PermissionError:return self.reply(401,{'error':{'message':'Device session expired'}})
        except (ValueError,UnicodeDecodeError,TimeoutError):self.reply(400,{'error':{'message':'Invalid request body'}})
    do_GET=handle_request
    do_POST=handle_request

def pairing_payload(origin):
    import base64,qrcode,qrcode.image.svg,time
    url=origin+'/#pair='+codex_devices.issue()
    svg=qrcode.make(url,image_factory=qrcode.image.svg.SvgPathFillImage).to_string()
    return {'url':url,'expires_at':time.time()+600,'qr':'data:image/svg+xml;base64,'+base64.b64encode(svg).decode()}


class LocalPairingHandler(Handler):
    """Coordinator-only issuer. Test containers cannot use loopback as authority."""
    def do_GET(self):self.reply(405,{'error':{'message':'POST required'}})
    def do_POST(self):
        if self.path!='/pairing':return self.reply(404,{'error':{'message':'Not found'}})
        if self.headers.get_all('Host',[])!=['127.0.0.1:8092'] or self.headers.get('Origin') is not None:
            return self.reply(403,{'error':{'message':'Native coordinator request required'}})
        if not authorized(self.headers,'pairing'):return self.reply(401,{'error':{'message':'Pairing issuer credential required'}})
        try:
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type')!='application/json':raise ValueError()
            if len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
            size=int(self.headers['Content-Length'])
            if not 0<size<=16:raise ValueError()
            self.connection.settimeout(5)
            if json.loads(self.rfile.read(size))!={}:raise ValueError()
            self.reply(200,pairing_payload(self.server.public_origin))
        except (ValueError,TimeoutError):self.reply(400,{'error':{'message':'Invalid pairing request'}})


if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='revoke-devices':
        codex_devices.revoke();print('All paired devices revoked');sys.exit(0)
    if len(sys.argv)>1 and sys.argv[1]=='pair':
        origin=os.getenv('CODEX_PUBLIC_ORIGIN','https://compai.lifetech.fyi')
        path=Path.home()/'.config/map-master/codex-pairing-link'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(origin+'/#pair='+codex_devices.issue()+'\n')
        path.chmod(0o600);print('One-time setup link saved to '+str(path)+' (expires in ten minutes)');sys.exit(0)
    origin=os.environ['CODEX_PUBLIC_ORIGIN']
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:raise ValueError('Configure one HTTPS origin')
    server=ThreadingHTTPServer(('127.0.0.1',8091),Handler);server.daemon_threads=True;server.public_origin=origin
    import threading
    issuer=ThreadingHTTPServer(('127.0.0.1',8092),LocalPairingHandler);issuer.daemon_threads=True;issuer.public_origin=origin
    threading.Thread(target=issuer.serve_forever,daemon=True).start()
    server.serve_forever()
