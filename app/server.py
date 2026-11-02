"""Bounded standard-library HTTP adapter with cookie auth and CSRF checks."""
from collections import defaultdict, deque
from email import policy
from email.parser import BytesParser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs, quote

from .auth import AuthenticationError, AuthorizationError
from .platform import EmailDeliveryError
from .storage import StorageError

WEB = Path(__file__).with_name('web')
MAX_BODY=29*1024*1024


class Server(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=32
    def __init__(self, address, service, *, origin=None, secure=False, trusted_proxy=False):
        self.service=service
        self.secure=secure
        self.trusted_proxy=trusted_proxy
        self.origin=(origin or f'http://{address[0]}:{address[1]}').rstrip('/')
        self.slots=threading.BoundedSemaphore(16)
        self.rates=defaultdict(deque); self.rate_lock=threading.Lock()
        super().__init__(address,Handler)
        if not origin: self.origin=f'http://{address[0]}:{self.server_port}'

    def process_request_thread(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'); self.shutdown_request(request); return
        try: super().process_request_thread(request,client_address)
        finally: self.slots.release()

    def limited(self, ip, path):
        cap=6 if path in ('request_code','verify_code','register','login') else 90
        now=time.monotonic(); key=(ip,path if cap==6 else 'all')
        with self.rate_lock:
            if len(self.rates)>10000:
                for old in list(self.rates):
                    if not self.rates[old] or self.rates[old][-1]<now-60: del self.rates[old]
            q=self.rates[key]
            while q and q[0]<now-60: q.popleft()
            if len(q)>=cap: return True
            q.append(now)
        return False


class Handler(BaseHTTPRequestHandler):
    server_version='PolicyPlatform/2'
    def setup(self):
        super().setup(); self.connection.settimeout(200)

    def log_message(self,*args):
        # No URL query, request bodies, cookies, provider errors or credentials.
        pass

    def respond(self, raw, code=200, mime='application/json; charset=utf-8', headers=None):
        self.send_response(code)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if self.server.secure: self.send_header('Strict-Transport-Security','max-age=31536000')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(raw)

    def json(self,data,code=200,headers=None):
        self.respond(json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),code,headers=headers)

    def token(self):
        jar=cookies.SimpleCookie()
        try: jar.load(self.headers.get('Cookie',''))
        except cookies.CookieError: return ''
        return jar['session'].value if 'session' in jar else ''

    def csrf(self,token):
        return hmac.new(self.server.service.platform.code_pepper.encode(),('csrf:'+token).encode(),hashlib.sha256).hexdigest()

    def principal(self):
        p=self.server.service.platform.authenticate(self.token())
        if not p: raise AuthenticationError('Login required')
        return p

    def cookie(self,token,delete=False):
        return 'session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('0' if delete else '2592000')+('; Secure' if self.server.secure else '')

    def do_GET(self):
        try:
            parsed=urlsplit(self.path); path=parsed.path
            if path=='/health': self.json({'status':'ok'}); return
            if path in ('/','/index.html','/app.js','/style.css'):
                name='index.html' if path=='/' else path[1:]
                mime={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[1]]
                self.respond((WEB/name).read_bytes(),mime=mime); return
            p=self.principal(); args={k:v[0] for k,v in parse_qs(parsed.query).items()}
            service=self.server.service
            if path=='/api/state':
                data=service.state(p,args.get('project_id'),args.get('conversation_id')); data['csrf']=self.csrf(self.token()); self.json(data)
            elif path=='/api/file':
                row,file_path,_=service.file_record(p,args)
                self.respond(file_path.read_bytes(),mime='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(row['source_name'])})
            elif path=='/api/source':
                scope=args.get('scope','public'); pid=args.get('project_id') if scope=='project' else None
                if scope=='public':
                    from .legacy import engine
                    from contextlib import closing
                    leg=engine(service.public_path)
                    with closing(leg.db()) as con:
                        doc=con.execute('SELECT title FROM documents WHERE planid=?',(args.get('planid'),)).fetchone()
                        if not doc: raise AuthorizationError('resource not found')
                        chunks=[dict(r) for r in con.execute('SELECT heading,ordinal,fragment,text FROM chunks WHERE planid=? ORDER BY fragment',(args['planid'],))]
                    self.json({'title':doc[0],'chunks':chunks,'note':'公共库原始Markdown未随交付包提供；此处显示库内原文片段。'})
                elif scope in ('private','project'):
                    if scope=='project' and not pid: raise ValueError('project_id required')
                    from contextlib import closing
                    kb=service.store(p,pid)
                    with closing(kb.connect()) as con:
                        doc=con.execute('SELECT title FROM documents WHERE document_id=?',(args.get('planid'),)).fetchone()
                        if not doc: raise AuthorizationError('resource not found')
                        chunks=[dict(r) for r in con.execute('SELECT heading,ordinal,fragment,text FROM chunks WHERE document_id=? ORDER BY fragment',(args['planid'],))]
                    self.json({'title':doc[0],'chunks':chunks})
                else: raise ValueError('Invalid scope')
            else: self.json({'error':'Not found'},404)
        except Exception as exc: self.error(exc)

    def body(self):
        if self.headers.get('Transfer-Encoding'): raise ValueError('Transfer-Encoding unsupported')
        size=int(self.headers.get('Content-Length','0'))
        if size<=0 or size>MAX_BODY: raise ValueError('Invalid request body size')
        raw=self.rfile.read(size)
        if len(raw)!=size: raise ValueError('Incomplete request')
        content_type=self.headers.get('Content-Type','')
        if content_type.startswith('application/json'):
            data=json.loads(raw)
            if not isinstance(data,dict): raise ValueError('JSON object required')
            return data
        if content_type.startswith('multipart/form-data'):
            message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
            data={}
            if not message.is_multipart(): raise ValueError('Invalid multipart body')
            for part in message.iter_parts():
                name=part.get_param('name',header='content-disposition')
                if name=='file':
                    data['filename']=part.get_filename(); data['content_base64']=base64.b64encode(part.get_payload(decode=True)).decode()
                elif name:
                    value=part.get_payload(decode=True).decode('utf-8')
                    data[name]=json.loads(value) if name in ('metadata','model_config','embedding_config') else value
            return data
        raise ValueError('application/json or multipart/form-data required')

    def do_POST(self):
        try:
            if urlsplit(self.path).path!='/api/action': self.json({'error':'Not found'},404); return
            # Browser must use the configured same origin. Nonbrowser callers
            # also provide it deliberately; proxy headers are never trusted.
            if self.headers.get('Origin','')!=self.server.origin: raise AuthorizationError('Origin rejected')
            data=self.body(); action=data.get('action','')
            client_ip=self.client_address[0]
            if self.server.trusted_proxy:
                try: client_ip=str(ipaddress.ip_address(self.headers.get('X-Real-IP','')))
                except ValueError: pass
            if self.server.limited(client_ip,action): self.json({'error':'Too many requests'},429); return
            pform=self.server.service.platform
            if action=='register':
                token,_=pform.register_password(str(data.get('email','')),data.get('password'))
                p=pform.authenticate(token)
                self.json({'user':{'user_id':p.user_id,'email':p.email},'csrf':self.csrf(token)},headers={'Set-Cookie':self.cookie(token)}); return
            if action=='login':
                token,_=pform.login_password(str(data.get('email','')),data.get('password'))
                p=pform.authenticate(token)
                self.json({'user':{'user_id':p.user_id,'email':p.email},'csrf':self.csrf(token)},headers={'Set-Cookie':self.cookie(token)}); return
            if action=='request_code':
                pform.request_code(str(data.get('email','')),str(data.get('purpose','login'))); self.json({'ok':True}); return
            if action=='verify_code':
                token,_=pform.verify_code(str(data.get('email','')),str(data.get('purpose','login')),str(data.get('code','')))
                p=pform.authenticate(token)
                self.json({'user':{'user_id':p.user_id,'email':p.email},'csrf':self.csrf(token)},headers={'Set-Cookie':self.cookie(token)}); return
            p=self.principal(); token=self.token()
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf(token)): raise AuthorizationError('CSRF rejected')
            if action=='logout':
                pform.logout(token); self.json({'ok':True},headers={'Set-Cookie':self.cookie('',True)}); return
            if action=='export':
                raw,name,mime=self.server.service.export(p,data)
                self.respond(raw,mime=mime,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name)}); return
            self.json(self.server.service.action(p,data))
        except Exception as exc: self.error(exc)

    def error(self,exc):
        if isinstance(exc,AuthenticationError): self.json({'error':str(exc)},401)
        elif isinstance(exc,AuthorizationError): self.json({'error':'Resource unavailable or permission denied'},403)
        elif isinstance(exc,(ValueError,KeyError,TypeError,StorageError)): self.json({'error':str(exc) if isinstance(exc,(ValueError,StorageError)) else 'Invalid request'},400)
        elif isinstance(exc,EmailDeliveryError): self.json({'error':'验证码邮件发送失败，请检查 SMTP 配置或稍后重试。'},503)
        elif isinstance(exc,RuntimeError): self.json({'error':'模型或服务暂不可用；请检查配置、余额和网络后重试。'},502)
        else: self.json({'error':'服务处理失败，请稍后重试。'},500)


def main():
    from .service import Service
    root=Path(os.environ.get('APP_DATA_DIR',Path(__file__).resolve().parent.parent/'data_v2'))
    pepper=os.environ.get('AUTH_CODE_PEPPER','')
    if len(pepper)<32: raise SystemExit('Set AUTH_CODE_PEPPER to a random secret of at least 32 characters')
    host=os.environ.get('APP_HOST','127.0.0.1'); port=int(os.environ.get('APP_PORT','8766'))
    origin=os.environ.get('APP_ORIGIN',f'http://{host}:{port}').rstrip('/')
    secure=origin.startswith('https://')
    if not secure and host not in ('127.0.0.1','localhost','::1'): raise SystemExit('External deployment requires HTTPS APP_ORIGIN')
    if os.environ.get('LLM_API_KEY') or os.environ.get('EMBEDDING_API_KEY'):
        print('新版仅使用请求携带的用户模型配置；不会读取服务器的旧模型密钥。')
    import fcntl
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    runtime_lock=(root/'.runtime.lock').open('a+')
    try: fcntl.flock(runtime_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Another V2 process or backup owns APP_DATA_DIR')
    trusted_proxy=os.environ.get('APP_TRUST_PROXY','').lower() in ('1','true','yes')
    server=Server((host,port),Service(root,pepper=pepper,public_path=os.environ.get('PUBLIC_KB_PATH')),origin=origin,secure=secure,trusted_proxy=trusted_proxy)
    print(f'管策智研 V2: {origin} | data: {root.resolve()}')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close(); runtime_lock.close()
