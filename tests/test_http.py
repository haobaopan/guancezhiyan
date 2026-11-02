"""Real loopback HTTP tests; SMTP and external model calls are not exercised."""
import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from app.server import Server
from app.service import Service


class FakeMailer:
    def __init__(self): self.codes={}
    def send_code(self,email,code,purpose): self.codes[(email,purpose)]=code


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.mail=FakeMailer()
        self.pepper='http-test-pepper-only-not-a-live-secret'
        self.start()
        self.addCleanup(self.cleanup)

    def start(self):
        service=Service(Path(self.tmp.name),mailer=self.mail,pepper=self.pepper)
        self.server=Server(('127.0.0.1',0),service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def cleanup(self):
        self.stop(); self.tmp.cleanup()

    def request(self,path='/api/action',*,data=None,cookie='',csrf='',origin=True):
        con=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={}
        if cookie: headers['Cookie']=cookie
        if csrf: headers['X-CSRF-Token']=csrf
        if origin: headers['Origin']=self.server.origin if origin is True else origin
        if data is not None:
            headers['Content-Type']='application/json'; body=json.dumps(data).encode(); method='POST'
        else: body=None; method='GET'
        try:
            con.request(method,path,body,headers); response=con.getresponse(); raw=response.read(); status=response.status
            response_headers=dict(response.getheaders())
            result=json.loads(raw) if response_headers.get('Content-Type','').startswith('application/json') else raw
            return status,result,response_headers
        finally: con.close()

    def action(self,auth,action,**data):
        return self.request(data=dict(data,action=action),cookie=auth['cookie'],csrf=auth['csrf'])

    def register(self,email):
        status,_,_=self.request(data={'action':'request_code','email':email,'purpose':'register'}); self.assertEqual(status,200)
        status,data,headers=self.request(data={'action':'verify_code','email':email,'purpose':'register','code':self.mail.codes[(email,'register')]})
        self.assertEqual(status,200); self.assertIn('HttpOnly',headers['Set-Cookie']); self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        return dict(data,cookie=headers['Set-Cookie'].split(';',1)[0])

    def test_email_password_auth_and_entry_page(self):
        status,_,_=self.request(data={'action':'register','email':'pw@example.cn','password':'a-secure-password'})
        self.assertEqual(status,200)
        status,_,_=self.request(data={'action':'register','email':'pw@example.cn','password':'another-secure-password'})
        self.assertEqual(status,400)
        status,_,_=self.request(data={'action':'login','email':'pw@example.cn','password':'wrong-password'})
        self.assertEqual(status,401)
        status,data,headers=self.request(data={'action':'login','email':'PW@example.cn','password':'a-secure-password'})
        self.assertEqual(status,200)
        self.assertEqual(data['user']['email'],'pw@example.cn')
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        status,html,_=self.request('/')
        self.assertEqual(status,200)
        self.assertIn(b'type="password"',html)
        self.assertNotIn('获取验证码'.encode(),html)

    def test_password_validation_and_existing_code_account_without_password(self):
        status,_,_=self.request(data={'action':'register','email':'short@example.cn','password':'short'})
        self.assertEqual(status,400)
        self.register('legacy@example.cn')
        status,_,_=self.request(data={'action':'login','email':'legacy@example.cn','password':'long-enough-password'})
        self.assertEqual(status,401)

    def test_register_login_logout_csrf_origin_and_revocation(self):
        a=self.register('a@example.cn')
        status,state,_=self.request('/api/state',cookie=a['cookie']); self.assertEqual(status,200); self.assertEqual(state['csrf'],a['csrf'])
        status,_,_=self.request(data={'action':'project_create','name':'blocked'},cookie=a['cookie']); self.assertEqual(status,403)
        status,_,_=self.request(data={'action':'project_create','name':'blocked'},cookie=a['cookie'],csrf=a['csrf'],origin='https://evil.example'); self.assertEqual(status,403)
        status,_,headers=self.action(a,'logout'); self.assertEqual(status,200); self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/state',cookie=a['cookie'])[0],401)
        self.assertEqual(self.action(a,'project_create',name='revoked')[0],401)
        self.request(data={'action':'request_code','email':'a@example.cn','purpose':'login'})
        status,data,headers=self.request(data={'action':'verify_code','email':'a@example.cn','purpose':'login','code':self.mail.codes[('a@example.cn','login')]})
        self.assertEqual(status,200); self.assertNotEqual(headers['Set-Cookie'].split(';')[0],a['cookie'])

    def test_file_source_project_export_ownership_and_restart(self):
        a=self.register('a@example.cn'); b=self.register('b@example.cn')
        status,project,_=self.action(a,'project_create',name='项目A'); self.assertEqual(status,200); pid=project['project_id']
        status,conv,_=self.action(a,'conversation_create',project_id=pid); self.assertEqual(status,200); cid=conv['conversation_id']
        status,_,_=self.action(b,'conversation_create',project_id=pid); self.assertEqual(status,403)
        text='# 农业政策\n\n农业产业发展应提高粮食生产能力。'
        status,uploaded,_=self.action(a,'upload_file',scope='project',project_id=pid,filename='政策.md',content_base64=base64.b64encode(text.encode()).decode())
        self.assertEqual(status,200); fid=uploaded['file_id']
        fileurl='/api/file?'+urlencode({'scope':'project','project_id':pid,'file_id':fid})
        self.assertEqual(self.request(fileurl,cookie=a['cookie'])[1],text.encode())
        self.assertEqual(self.request(fileurl,cookie=b['cookie'])[0],403)
        source='/api/source?'+urlencode({'scope':'project','project_id':pid,'planid':fid})
        self.assertEqual(self.request(source,cookie=a['cookie'])[0],200)
        self.assertEqual(self.request(source,cookie=b['cookie'])[0],403)
        status,answer,_=self.action(a,'send_message',conversation_id=cid,project_id=pid,mode='ask',scope='project',content='农业产业发展')
        self.assertEqual(status,200); self.assertTrue(answer['sources']); self.assertEqual(answer['sources'][0]['scope'],'project')
        rid=answer['export_id']
        for fmt in ('docx','xlsx','pptx','csv'):
            status,raw,headers=self.action(a,'export',export_id=rid,format=fmt)
            self.assertEqual(status,200); self.assertTrue(raw)
            self.assertIn('attachment',headers['Content-Disposition'])
            self.assertEqual(self.action(b,'export',export_id=rid,format=fmt)[0],403)
        query='/api/state?'+urlencode({'project_id':pid,'conversation_id':cid})
        status,state,_=self.request(query,cookie=a['cookie']); self.assertEqual(status,200); self.assertEqual(len(state['messages']),2)
        self.assertEqual(self.request(query,cookie=b['cookie'])[0],403)
        self.stop(); self.start()
        status,state,_=self.request(query,cookie=a['cookie']); self.assertEqual(status,200); self.assertEqual(len(state['messages']),2)
        self.assertEqual(self.request(fileurl,cookie=a['cookie'])[1],text.encode())
        self.assertEqual(self.action(a,'export',export_id=rid,format='csv')[0],200)
        self.assertEqual(self.action(a,'project_delete',project_id=pid)[0],200)
        self.assertEqual(self.request(source,cookie=a['cookie'])[0],403)
        self.assertEqual(self.request(query,cookie=a['cookie'])[0],403)
        self.assertEqual(self.action(a,'export',export_id=rid,format='csv')[0],403)

    def test_code_failure_single_use_and_unauthed_state(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.request(data={'action':'request_code','email':'new@example.cn','purpose':'register'})
        code=self.mail.codes[('new@example.cn','register')]
        status,_,_=self.request(data={'action':'verify_code','email':'new@example.cn','purpose':'register','code':'bad'}); self.assertEqual(status,401)
        payload={'action':'verify_code','email':'new@example.cn','purpose':'register','code':code}
        self.assertEqual(self.request(data=payload)[0],200)
        self.assertEqual(self.request(data=payload)[0],401)

if __name__=='__main__': unittest.main()
