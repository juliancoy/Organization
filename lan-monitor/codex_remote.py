"""Narrow owner-authenticated Codex web service for an HTTPS tunnel.

Bind only to loopback. The tunnel publishes this service, never the test master.
No test, shell, proxy, or arbitrary-file endpoints are exposed.
"""
import json
import mimetypes
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import codex_context
from master_api import authorized

VIEW=Path(__file__).with_name('view')
ASSETS={'/':'codex.html','/codex.html':'codex.html','/codex.js':'codex.js','/codex.css':'codex.css','/style.css':'style.css'}
API='/api/v1/master/codex/'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass  # Never retain credentials, URLs, or conversation text.
    def reply(self,status,value,kind='application/json'):
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.send_response(status)
        for k,v in {'Content-Type':kind,'Content-Length':str(len(raw)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",'Strict-Transport-Security':'max-age=31536000'}.items():self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def handle_request(self):
        origin=self.server.public_origin
        if len(self.headers.get_all('Host',[]))!=1 or self.headers['Host']!=urlsplit(origin).netloc:
            return self.reply(403,{'error':{'message':'Host denied'}})
        if self.headers.get('Origin') not in (None,origin):return self.reply(403,{'error':{'message':'Origin denied'}})
        path=urlsplit(self.path).path
        if self.command=='GET' and path in ASSETS:
            name=ASSETS[path];return self.reply(200,(VIEW/name).read_bytes(),mimetypes.guess_type(name)[0] or 'application/octet-stream')
        valid_read=self.command=='GET' and (path==API+'threads' or path.startswith(API+'threads/'))
        valid_write=self.command=='POST' and path==API+'commands'
        if not (valid_read or valid_write):return self.reply(404,{'error':{'message':'Not found'}})
        if not authorized(self.headers,'codex'):return self.reply(401,{'error':{'message':'Codex owner token required'}})
        body=None
        try:
            if valid_write:
                if self.headers.get('Origin')!=origin:return self.reply(403,{'error':{'message':'Same-origin browser required'}})
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise ValueError()
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError()
                size=int(self.headers.get('Content-Length','-1'))
                if not 0<size<=65536:raise ValueError()
                self.connection.settimeout(10)
                raw=self.rfile.read(size)
                if len(raw)!=size:raise ValueError()
                body=json.loads(raw)
            code,value=codex_context.rest(self.command,path,parse_qs(urlsplit(self.path).query),body)
            self.reply(code,value)
        except (ValueError,UnicodeDecodeError,TimeoutError):self.reply(400,{'error':{'message':'Invalid request body'}})
    do_GET=handle_request
    do_POST=handle_request

if __name__=='__main__':
    origin=os.environ['CODEX_PUBLIC_ORIGIN']
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:raise ValueError('Configure one HTTPS origin')
    server=ThreadingHTTPServer(('127.0.0.1',8091),Handler);server.daemon_threads=True;server.public_origin=origin
    server.serve_forever()
