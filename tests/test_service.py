import base64
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.auth import Principal, AuthorizationError
from app.service import Service


class FakeClient:
    def __init__(self, config):
        self.config=config
    def embed(self, texts):
        dim=int(self.config.get('dimensions',2))
        return [[1.]+[0.]*(dim-1) for _ in texts]


class ServiceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service=Service(Path(self.tmp.name),pepper='test-pepper')
        import uuid
        self.a=Principal(str(uuid.uuid4()),'a@example.test')
        self.b=Principal(str(uuid.uuid4()),'b@example.test')
        with self.service.db.transaction() as con:
            for p in (self.a,self.b):
                con.execute('INSERT INTO users(user_id,email,created_at,updated_at) VALUES(?,?,?,?)',(p.user_id,p.email,'t','t'))
        self.pa=self.service.platform.create_project(self.a,'a')['project_id']
        self.pb=self.service.platform.create_project(self.a,'b')['project_id']

    def upload(self,principal,text='农业政策证据',scope='private',pid=None,embedding=None):
        data={'scope':scope,'filename':'policy.md','content_base64':base64.b64encode(text.encode()).decode()}
        if pid: data['project_id']=pid
        if embedding: data['embedding_config']=embedding
        return self.service.action(principal,dict(data,action='upload_file'))

    def test_existing_public_corpus_retrieval_remains_available(self):
        refs,warnings=self.service.retrieve(self.a,{'scope':'public'},'十四五 乡村振兴')
        self.assertEqual([str(r['planid']) for r in refs[:3]],['24378','29541','20115'])
        self.assertTrue(all(r['scope']=='public' and r['text'] for r in refs))
        self.assertTrue(warnings)  # No BGE key: lexical fallback is explicit.

    def test_private_file_owner_scope_search_and_delete(self):
        doc=self.upload(self.a)
        self.assertEqual(len(self.service.store(self.a).search('农业')),1)
        self.assertEqual(self.service.store(self.b).search('农业'),[])
        req={'scope':'private','file_id':doc['file_id']}
        with self.assertRaises(AuthorizationError): self.service.file_record(self.b,req)
        with self.assertRaises(AuthorizationError): self.service.delete_file(self.b,req)
        self.service.delete_file(self.a,req)
        self.assertEqual(self.service.store(self.a).search('农业'),[])
        self.assertEqual(self.service.state(self.a)['private_files'],[])

    def test_project_files_never_enter_global_or_other_project(self):
        doc=self.upload(self.a,scope='project',pid=self.pa)
        self.assertEqual(self.service.store(self.a).search('农业'),[])
        self.assertEqual(self.service.store(self.a,self.pb).search('农业'),[])
        self.assertEqual(len(self.service.store(self.a,self.pa).search('农业')),1)
        with self.assertRaises(AuthorizationError): self.service.store(self.b,self.pa)
        with self.assertRaises(AuthorizationError):
            self.service.file_record(self.a,{'scope':'project','project_id':self.pb,'file_id':doc['file_id']})

    def test_private_retrieve_converts_complete_evidence_payload(self):
        self.upload(self.a)
        refs,_=self.service.retrieve(self.a,{'scope':'private'},'农业')
        self.assertEqual(len(refs),1)
        self.assertEqual(refs[0]['scope'],'private')
        self.assertEqual(refs[0]['id'],1)
        self.assertNotIn('source_path',refs[0])
        self.assertNotIn('embedding',refs[0])
        self.assertEqual(self.service.retrieve(self.b,{'scope':'private'},'农业')[0],[])

    def test_mixed_retrieval_keeps_corpus_sources_and_user_isolation(self):
        self.upload(self.a,text='农业私人材料')
        from app.legacy import engine as legacy_engine
        def engine_stub(*args, **kwargs):
            instance=legacy_engine(*args,**kwargs)
            def retrieve(query):
                if '农业' not in query: return []
                return [{'id':1,'planid':'public-document','source_name':'公开政策','title':'公开政策','filename':'public.md','area_name':'','fyp_name':'','heading':'正文','ordinal':1,'fragment':1,'text':'农业公共证据','score':0.1,'source_path':'public.md','top_category':'农业农村','subcategory':'','subsubcategory':''}]
            instance.retrieve=retrieve
            return instance
        with patch('app.service.engine',engine_stub):
            refs,warnings=self.service.retrieve(self.a,{'scope':'both'},'农业')
            self.assertEqual({r['scope'] for r in refs},{'public','private'})
            self.assertEqual([r['id'] for r in refs],[1,2])
            other,_=self.service.retrieve(self.b,{'scope':'both'},'农业')
            self.assertEqual([r['scope'] for r in other],['public'])
        # This test stubs public recall only; provider calls and public ranking
        # have separate regression checks and are not asserted here.

    def test_reindex_dimension_switch_and_owner_scope(self):
        with patch('app.service.Client',FakeClient):
            self.upload(self.a,embedding={'model':'old','dimensions':2})
            result=self.service.action(self.a,{'action':'reindex','scope':'private','embedding_config':{'model':'new','dimensions':3}})
            self.assertEqual(result['chunks'],1)
        with closing(self.service.store(self.a).connect()) as con:
            row=con.execute('SELECT embedding_model,length(embedding) FROM chunks').fetchone()
            self.assertEqual(tuple(row),(result['model'],12))
        self.assertEqual(self.service.store(self.b).list_documents(),[])


    def test_chat_context_utf8_budget_current_once_and_history_roles(self):
        cid=self.service.platform.create_conversation(self.a,self.pa)
        self.service.platform.add_message(self.a,cid,'user','旧问题'+('中文'*500))
        self.service.platform.add_message(self.a,cid,'assistant','旧回答'+('历史'*500))
        content='上次为什么采用唯一标记current_question的方案'
        self.service.platform.add_message(self.a,cid,'user',content)
        refs=[{'id':1,'filename':'材料','heading':'章节','text':'证据'*2000}]
        messages=self.service.chat_context(self.a,self.pa,cid,content,refs,4000)
        import json
        serialized=json.dumps(messages,ensure_ascii=False)
        self.assertLessEqual(len(serialized.encode('utf-8')),4000)
        self.assertEqual(serialized.count('current_question'),1)
        self.assertEqual(messages[-1],{'role':'user','content':content})
        with self.assertRaises(ValueError):
            self.service.chat_context(self.a,self.pa,cid,'中文'*3000,[],4000)

if __name__=='__main__': unittest.main()
