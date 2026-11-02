import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from app.database import Database
from app.auth import Principal, AuthenticationError, AuthorizationError
from app.platform import Platform, now_iso
from app.memory import MemoryService

class Mailer:
    def send_code(self,email,code,purpose): self.code=code

class Client:
    def chat(self,messages):
        return json.dumps({'summary':'数据库方案已确认，下一步实现','project_summary':'SQLite 项目','memories':[{'scope':'project','category':'decision','content':'数据库使用 SQLite','source_quote':'数据库使用 SQLite','importance':.9,'confidence':1}]})

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        db=Database(Path(self.tmp.name)/'app.sqlite3'); db.initialize()
        self.mail=Mailer(); self.platform=Platform(db,mailer=self.mail,code_pepper='test-only-pepper'); self.memory=MemoryService(self.platform)
        with db.transaction() as con:
            for uid in ['a','b']:
                con.execute('INSERT INTO users(user_id,email,created_at,updated_at) VALUES(?,?,?,?)',(uid,uid+'@example.cn',now_iso(),now_iso()))
        self.a=Principal('a'); self.b=Principal('b')
        self.pid=self.platform.create_project(self.a,'项目A')['project_id']; self.pid2=self.platform.create_project(self.a,'项目B')['project_id']
        self.cid=self.platform.create_conversation(self.a,self.pid)

    def test_wrong_codes_commit_and_resend_invalidates(self):
        self.platform.request_code('a@example.cn','login'); old=self.mail.code
        for _ in range(5):
            with self.assertRaises(AuthenticationError): self.platform.verify_code('a@example.cn','login','bad')
        with self.assertRaises(AuthenticationError): self.platform.verify_code('a@example.cn','login',old)
        self.platform.request_code('a@example.cn','login')
        token,_=self.platform.verify_code('a@example.cn','login',self.mail.code)
        self.assertEqual(self.platform.authenticate(token).user_id,'a')
        with self.assertRaises(AuthenticationError): self.platform.verify_code('a@example.cn','login',old)

    def test_order_and_explicit_conversation(self):
        for value in ['one','two','three']: self.platform.add_message(self.a,self.cid,'user',value)
        other=self.platform.create_conversation(self.a,self.pid)
        self.platform.add_message(self.a,other,'user','unrelated')
        ctx=self.memory.context(self.a,self.pid,self.cid,'one')
        self.assertEqual([m['content'] for m in ctx['recent_messages']],['one','two','three'])
        with self.assertRaises(AuthorizationError): self.memory.context(self.a,self.pid2,self.cid,'one')
        with self.assertRaises(AuthorizationError): self.memory.context(self.b,self.pid,self.cid,'one')

    def test_scope_and_progressive_raw(self):
        self.memory.save(self.a,'数据库使用 SQLite',project_id=self.pid)
        self.memory.save(self.a,'数据库使用 MySQL',project_id=self.pid2)
        self.platform.add_message(self.a,self.cid,'user','上次配置 Nginx 错误解决')
        ctx=self.memory.context(self.a,self.pid,self.cid,'数据库方案')
        self.assertEqual(len(ctx['project_memory']),1); self.assertNotIn('MySQL',str(ctx))
        self.assertEqual(ctx['raw_conversation'],[])
        self.assertTrue(self.memory.context(self.a,self.pid,self.cid,'上次 Nginx')['raw_conversation'])

    def test_budget(self):
        self.platform.add_message(self.a,self.cid,'user','需要保留的要求'*100)
        ctx=self.memory.context(self.a,self.pid,self.cid,'要求',budget=1000)
        self.assertLessEqual(ctx['used_chars'],1000)
        self.assertTrue(ctx['recent_messages'])
        self.platform.add_message(self.a,self.cid,'user','要求'*1000)
        with self.assertRaises(ValueError): self.memory.context(self.a,self.pid,self.cid,'要求',budget=1000)

    def test_extraction_job_dedup_and_toggle(self):
        self.platform.add_message(self.a,self.cid,'user','请记住数据库使用 SQLite')
        first=self.memory.process(self.a,self.cid,Client())
        self.assertEqual(first['status'],'succeeded')
        self.assertEqual(self.memory.process(self.a,self.cid,Client())['status'],'skipped')
        self.assertEqual(len(self.memory.list(self.a,self.pid)),1)
        self.memory.toggle(self.a,False)
        self.platform.add_message(self.a,self.cid,'user','请记住数据库变化')
        self.assertEqual(self.memory.process(self.a,self.cid,Client())['reason'],'disabled')

    def test_crud_superseded_sensitive_and_deleted_project(self):
        mid=self.memory.save(self.a,'数据库方案',project_id=self.pid)
        new=self.memory.update(self.a,mid,'更新数据库方案')
        self.assertEqual([r['memory_id'] for r in self.memory.list(self.a,self.pid)],[new])
        with self.assertRaises(ValueError): self.memory.save(self.a,'API_KEY=secret',project_id=self.pid)
        with self.assertRaises(AuthorizationError): self.memory.delete(self.b,new)
        self.platform.delete_project(self.a,self.pid)
        with self.assertRaises(AuthorizationError): self.platform.get_conversation(self.a,self.cid)
        with self.assertRaises(AuthorizationError): self.platform.add_message(self.a,self.cid,'user','x')

    def test_failure_retriable_no_credentials_in_job(self):
        self.platform.add_message(self.a,self.cid,'user','请记住数据库使用 SQLite')
        class Bad:
            def chat(self,m): raise RuntimeError('sk-credential-should-not-leak')
        result=self.memory.process(self.a,self.cid,Bad())
        self.assertEqual(result['status'],'failed')
        with closing(self.platform.db.connect()) as con:
            job=dict(con.execute('SELECT * FROM memory_jobs').fetchone())
        self.assertNotIn('credential',str(job)); self.assertEqual(job['attempts'],1)
        self.assertEqual(self.memory.process(self.a,self.cid,Client())['job_id'],result['job_id'])

    def test_chinese_bearer_and_jwt_credentials_blocked(self):
        from app.platform import re_secret
        for value in ['密钥：abc123', '密码是 test-password', '令牌为 aaa111',
                      'Authorization: Bearer abcdefgh', 'Bearer ABC123xyz',
                      'sk-1234567890123456', 'eyJhbGciOiJIUzI1NiJ9.abc123.xyz123',
                      '-----BEGIN PRIVATE KEY-----']:
            self.assertTrue(re_secret(value),value)
            with self.assertRaises(ValueError): self.memory.save(self.a,value)
        self.assertFalse(re_secret('API Key 不应进入长期记忆'))

    def test_current_query_not_retrieved_again_as_raw_history(self):
        question='上次的不同数据库方案是什么'
        self.platform.add_message(self.a,self.cid,'user',question)
        ctx=self.memory.context(self.a,self.pid,self.cid,question)
        self.assertEqual(ctx['raw_conversation'],[])
        self.assertEqual(len(ctx['recent_messages']),1)

    def test_extractor_preserves_old_project_summary_and_requires_user_quote(self):
        self.memory.summary(self.a,self.pid,'历史决策：使用 SQLite，禁止海外依赖')
        self.platform.add_message(self.a,self.cid,'user','请记住采用 SQLite')
        self.platform.add_message(self.a,self.cid,'assistant','建议改用 MySQL，尚未确认')
        class Capture:
            prompt=''
            def chat(client,messages):
                client.prompt=messages[0]['content']
                return json.dumps({'summary':'本次确认SQLite，MySQL仅建议','project_summary':'保留历史SQLite决策',
                    'memories':[{'scope':'project','category':'decision','content':'采用 MySQL','source_quote':'建议改用 MySQL，尚未确认'},
                                {'scope':'project','category':'decision','content':'采用 SQLite','source_quote':'采用 SQLite'}]})
        client=Capture(); result=self.memory.process(self.a,self.cid,client,force=True)
        self.assertEqual(result['status'],'succeeded')
        self.assertIn('禁止海外依赖',client.prompt)
        self.assertEqual([r['content'] for r in self.memory.list(self.a,self.pid)],['采用 SQLite'])

if __name__=='__main__': unittest.main()
