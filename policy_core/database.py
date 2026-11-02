"""Database operations for one policy engine instance."""
import sqlite3


class DatabaseMixin:
    def fts_terms(self, heading, title, text):
        return ' '.join(sorted(self.tokens(f'{heading} {title or ""} {text}')))


    def rebuild_fts(self, con, reason=''):
        """Rebuild the BM25 index from scratch.

    A plain (non contentless / non external-content) fts5 table rejects the
    'delete-all' and 'delete' special commands, so the index must be cleared
    with an ordinary DELETE statement.
    """
        try:
            con.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(terms)')
        except sqlite3.OperationalError as exc:
            self.FTS_AVAILABLE = False
            self.log.warning('FTS5 不可用，关键词检索将退回 Python 兜底：%s', exc)
            return 0
        con.execute('DELETE FROM chunks_fts')
        done = 0
        for r in con.execute('SELECT c.id,c.heading,c.text,d.title FROM chunks c '
                             'LEFT JOIN documents d ON d.planid=c.planid'):
            con.execute('INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)',
                        (r['id'], self.fts_terms(r['heading'], r['title'], r['text'])))
            done += 1
        con.commit()
        self.log.info('FTS 索引重建完成：%s 条切片%s', done, f'（{reason}）' if reason else '')
        return done


    def _init_db(self, con):
        """Create or migrate the schema once per process, and guarantee a live FTS index."""
        con.executescript('''CREATE TABLE IF NOT EXISTS documents (planid TEXT PRIMARY KEY, split TEXT NOT NULL, title TEXT NOT NULL, area_name TEXT, admin_level TEXT, parent_area_name TEXT, fyp_name TEXT, plantype_label TEXT, library_fileext TEXT, content_source TEXT, source_path TEXT NOT NULL, status TEXT NOT NULL, top_category TEXT NOT NULL, subcategory TEXT, subsubcategory TEXT, source_layer TEXT NOT NULL, text_len INTEGER NOT NULL, source_name TEXT NOT NULL DEFAULT ''); CREATE TABLE IF NOT EXISTS chunks (
      id INTEGER PRIMARY KEY, planid TEXT, filename TEXT NOT NULL, heading TEXT NOT NULL,
      ordinal INTEGER NOT NULL, fragment INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB, embedding_model TEXT, UNIQUE(planid, fragment))''')
        doc_cols={r[1] for r in con.execute('PRAGMA table_info(documents)')}
        if 'source_name' not in doc_cols: con.execute("ALTER TABLE documents ADD COLUMN source_name TEXT NOT NULL DEFAULT ''")
        cols={r[1] for r in con.execute('PRAGMA table_info(chunks)')}
        if 'fragment' not in cols:
            con.execute('ALTER TABLE chunks RENAME TO chunks_legacy')
            con.execute('''CREATE TABLE chunks (id INTEGER PRIMARY KEY, planid TEXT, filename TEXT NOT NULL, heading TEXT NOT NULL,
          ordinal INTEGER NOT NULL, fragment INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(planid, fragment))''')
            con.execute('INSERT INTO chunks(planid,filename,heading,ordinal,fragment,text) '
                        'SELECT planid,filename,heading,ordinal,ordinal,text FROM chunks_legacy')
            con.execute('DROP TABLE chunks_legacy')
            cols={r[1] for r in con.execute('PRAGMA table_info(chunks)')}
        if 'embedding' not in cols: con.execute('ALTER TABLE chunks ADD COLUMN embedding BLOB')
        if 'embedding_model' not in cols: con.execute('ALTER TABLE chunks ADD COLUMN embedding_model TEXT')
        con.execute('CREATE INDEX IF NOT EXISTS idx_chunks_planid ON chunks(planid)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_chunks_heading ON chunks(heading)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(top_category, subcategory, subsubcategory)')
        con.commit()
        # The index must exist AND cover every chunk; otherwise keyword search
        # silently returns nothing, which is far worse than a slow fallback.
        try:
            con.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(terms)')
            indexed=con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
            total=con.execute('SELECT count(*) FROM chunks').fetchone()[0]
            if indexed != total:
                self.log.warning('FTS 索引与切片数不一致（%s/%s），开始重建', indexed, total)
                self.rebuild_fts(con, f'{indexed}/{total} 不一致')
        except sqlite3.DatabaseError as exc:
            self.log.error('FTS 初始化失败，关键词检索退回 Python 兜底：%s', exc)


    def db(self):
        con = sqlite3.connect(self.DB, timeout=30)
        con.row_factory = sqlite3.Row
        if not self._DB_READY:
            with self._DB_INIT_LOCK:
                if not self._DB_READY:
                    self._init_db(con)
                    self._DB_READY = True
        return con


