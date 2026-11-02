import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from app.database import Database
from app.auth import AuthService, Principal, AuthorizationError, AuthenticationError, require_owner
from app.storage import Storage, StorageError
from app.models import Client, ModelError, validate_url

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'app.sqlite3');self.db.initialize()
        self.con=self.db.connect();self.a=str(uuid.uuid4());self.b=str(uuid.uuid4())
        for uid in [self.a,self.b]:self.con.execute("INSERT INTO users(user_id,email,created_at,updated_at) VALUES(?,?,?,?)",(uid,uid+'@example.cn','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00'))
        self.con.commit();self.auth=AuthService(self.db)
    def tearDown(self):self.con.close();self.tmp.cleanup()
    def test_session_lifecycle_and_no_plain_token(self):
        token,_=self.auth.create_session(self.con,self.a);self.con.commit()
        row=self.con.execute('SELECT * FROM sessions').fetchone();self.assertNotIn(token,tuple(row));self.assertEqual(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.auth.authenticate(self.con,token).user_id,self.a)
        self.assertTrue(self.auth.revoke_session(self.con,token));self.assertIsNone(self.auth.authenticate(self.con,token))
    def test_expired_and_disabled_user(self):
        token,_=self.auth.create_session(self.con,self.a);self.con.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'");self.assertIsNone(self.auth.authenticate(self.con,token))
        token,_=self.auth.create_session(self.con,self.b);self.con.execute("UPDATE users SET status='disabled' WHERE user_id=?",(self.b,));self.assertIsNone(self.auth.authenticate(self.con,token))
    def test_owner_and_whitelist(self):
        pid=str(uuid.uuid4());self.con.execute("INSERT INTO projects(project_id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",(pid,self.a,'项目','now','now'))
        self.assertEqual(require_owner(self.con,Principal(self.a),'projects',pid)['name'],'项目')
        for table in ['projects','projects; DROP TABLE users;--']:
            with self.assertRaises(AuthorizationError):require_owner(self.con,Principal(self.b),table,pid)
        with self.assertRaises(AuthenticationError):require_owner(self.con,None,'projects',pid)

class StorageTests(unittest.TestCase):
    def test_isolation_and_path_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Storage(temp);a=str(uuid.uuid4());b=str(uuid.uuid4());p=str(uuid.uuid4());p2=str(uuid.uuid4())
            f=store.save_user_file(a,b'hello','材料.md');self.assertEqual(store.resolve_user_file(a,f.storage_path).read_bytes(),b'hello')
            with self.assertRaises(StorageError):store.resolve_user_file(b,f.storage_path)
            with self.assertRaises(StorageError):store.resolve_user_file(a,'../outside')
            with self.assertRaises(StorageError):store.save_user_file(a,b'hello','../材料.md')
            with self.assertRaises(StorageError):store.save_user_file(a,b'hello','bad\\file.md')
            pf=store.save_project_file(a,p,b'project','资料.txt')
            with self.assertRaises(StorageError):store.resolve_project_file(a,p2,pf.storage_path)
            with self.assertRaises(StorageError):store.resolve_user_file(a,pf.storage_path)
            store.delete_project_file(a,p,pf.storage_path);self.assertFalse((Path(temp)/pf.storage_path).exists())
            self.assertEqual(list(Path(temp).rglob('.upload-*')),[])
    def test_quota_and_uuid_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Storage(temp,max_file_bytes=3)
            with self.assertRaises(StorageError):s.save_user_file(str(uuid.uuid4()),b'abcd','a.txt')
            with self.assertRaises(StorageError):s.save_user_file('../other',b'a','a.txt')

class ModelsTests(unittest.TestCase):
    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    def test_url_validation(self,dns):
        self.assertEqual(validate_url('https://api.siliconflow.cn/v1/'),'https://api.siliconflow.cn/v1')
        for bad in ['http://api.siliconflow.cn/v1','https://127.0.0.1/v1','https://evil.example/v1','https://u:p@api.siliconflow.cn/v1','https://api.siliconflow.cn:8765/v1']:
            with self.assertRaises(ModelError):validate_url(bad)
    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))])
    def test_private_dns_rejected(self,dns):
        with self.assertRaises(ModelError):validate_url('https://api.siliconflow.cn/v1')
    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    def test_embedding_response_validation(self,dns):
        c=Client({'base_url':'https://api.siliconflow.cn/v1','model':'BAAI/bge-m3','api_key':'secret-key','dimensions':2})
        self.assertNotIn('secret-key',repr(c))
        with patch.object(c,'_request',return_value={'data':[{'index':0,'embedding':[1,0]}]}):self.assertEqual(c.embed(['text']),[[1,0]])
        for vector in [[1],[0,0],[float('nan'),1]]:
            with patch.object(c,'_request',return_value={'data':[{'index':0,'embedding':vector}]}):
                with self.assertRaises(ModelError):c.embed(['text'])

    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    def test_provider_error_does_not_echo_credentials(self,dns):
        from urllib.error import HTTPError
        c=Client({'base_url':'https://api.siliconflow.cn/v1','model':'test','api_key':'do-not-echo'})
        error=HTTPError(c.base,401,'do-not-echo',{},None)
        with patch.object(c._opener,'open',side_effect=error):
            with self.assertRaises(ModelError) as caught:c.chat([{'role':'user','content':'ok'}])
        self.assertNotIn('do-not-echo',str(caught.exception))
        self.assertNotIn('api_key',c.config)
    def test_redirect_rejected(self):
        from app.models import _NoRedirect
        with self.assertRaises(ModelError):_NoRedirect().redirect_request(None,None,302,'redirect',{},'https://127.0.0.1')
    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))])
    def test_connection_dns_pin_rejects_private(self,dns):
        from app.models import _PinnedHTTPSConnection
        with self.assertRaises(ModelError):_PinnedHTTPSConnection('api.siliconflow.cn').connect()
    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    def test_configured_custom_host(self,dns):
        with patch.dict('os.environ',{'MODEL_ALLOWED_HOSTS':'models.example.cn'}):
            self.assertEqual(validate_url('https://models.example.cn/v1'),'https://models.example.cn/v1')

    @patch('app.models.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    def test_embedding_batches_and_safe_parameters(self,dns):
        c=Client({'base_url':'https://api.siliconflow.cn/v1','model':'test','api_key':'secret','parameters':{'top_p':.8}})
        def response(path,body):return {'data':[{'index':i,'embedding':[1,0]} for i,_ in enumerate(body['input'])]}
        with patch.object(c,'_request',side_effect=response) as req:
            self.assertEqual(len(c.embed(['x']*150)),150);self.assertEqual(req.call_count,3)
        with self.assertRaises(ModelError):Client({'base_url':'https://api.siliconflow.cn/v1','model':'test','api_key':'secret','parameters':{'model':'override'}})

if __name__=='__main__':unittest.main()
