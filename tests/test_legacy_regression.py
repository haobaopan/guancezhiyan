import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
import zipfile
import io

from app.legacy import engine


ROOT=Path(__file__).resolve().parents[1]


class PublicKnowledgeRegressionTests(unittest.TestCase):
    def test_public_queries_match_corpus_signature_read_only(self):
        path=ROOT/'knowledge.sqlite3'
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        leg=engine(path)
        expected={
            '十四五 乡村振兴':['24378','29541','20115'],
            '人才发展规划':['40804','37982','38366'],
            '高质量发展':['43276','32011','20464'],
            '生态环境保护':['29728','32524','41529'],
            '政府工作报告':['10510','15831','16176'],
        }
        for query, ids in expected.items():
            rows=leg.retrieve(query)
            self.assertEqual([str(r['planid']) for r in rows[:3]],ids,query)
            self.assertTrue(all(r['text'] and r['fragment'] for r in rows))
            self.assertTrue(all('embedding' not in r and 'embedding_model' not in r for r in rows))
        with closing(leg.db()) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("UPDATE documents SET title='mutated' WHERE planid='24378'")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_rule_extraction_and_export_artifacts(self):
        leg=engine(ROOT/'knowledge.sqlite3')
        rows=leg.retrieve('高质量发展')
        refs=leg.sources_payload(rows)
        extracted=leg.rule_elements(refs)
        self.assertTrue(any(extracted.values()))
        docx=leg.make_docx('离线回归', '仅基于已检索证据', refs)
        xlsx=leg.make_xlsx(['文件','原文'],[[r['filename'],r['text']] for r in refs[:2]])
        pptx=leg.make_pptx('离线回归','仅基于已检索证据',refs)
        for raw in (docx,xlsx,pptx):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertTrue(archive.namelist())
                self.assertTrue(archive.testzip() is None)


if __name__=='__main__':
    unittest.main()
