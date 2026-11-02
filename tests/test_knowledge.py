import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from app.knowledge import KnowledgeStore


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = KnowledgeStore(Path(self.tmp.name) / 'private.sqlite3')

    def put(self, text='农业农村政策', doc='one', embed=None):
        return self.store.index_text(doc,'政策',text,'original.txt',{'now':'2026-01-01','embedding_model':'old'},embed)

    def test_search_reupload_delete_leave_no_stale_fts(self):
        self.put()
        self.assertEqual(self.store.search('农业')[0]['document_id'], 'one')
        self.put('城市交通建设')
        self.assertEqual(self.store.search('农业'), [])
        self.assertEqual(self.store.search('交通')[0]['document_id'], 'one')
        self.assertTrue(self.store.delete_document('one'))
        self.assertEqual(self.store.search('交通'), [])
        with closing(self.store.connect()) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0], 0)

    def test_failed_reindex_retains_previous_vectors(self):
        self.put(embed=lambda texts:[[1.,0.] for _ in texts])
        with self.assertRaises(ValueError):
            self.store.reindex('new',lambda texts:[[0.,0.] for _ in texts])
        with closing(self.store.connect()) as con:
            self.assertEqual(con.execute('SELECT embedding_model FROM chunks').fetchone()[0], 'old')
        self.assertEqual(self.store.reindex('new',lambda texts:[[0.,1.,0.] for _ in texts]), 1)
        with closing(self.store.connect()) as con:
            row=con.execute('SELECT embedding_model,length(embedding) FROM chunks').fetchone()
            self.assertEqual(tuple(row),('new',12))
        self.assertEqual(len(self.store.search('other',embed=lambda texts:[[0.,1.,0.]],embedding_model='new')),1)
        status=self.store.index_status('new',3)
        self.assertTrue(status['compatible'])
        self.assertEqual(status['active']['dimensions'],3)
        self.assertEqual(status['active']['generation'],1)
        self.assertTrue(self.store.index_status('old',2)['warning'])

    def test_concurrent_document_change_prevents_switch(self):
        self.put(embed=lambda texts:[[1.,0.] for _ in texts])
        def embed(texts):
            self.put('城市交通建设')
            return [[1.,0.] for _ in texts]
        with self.assertRaisesRegex(ValueError,'knowledge changed'):
            self.store.reindex('new',embed)
        with closing(self.store.connect()) as con:
            self.assertIsNone(con.execute('SELECT embedding_model FROM chunks').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
