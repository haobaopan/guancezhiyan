"""Tenant-scoped SQLite RAG stores; public KB files are never opened here."""
from __future__ import annotations

import math
from contextlib import closing
import re
import sqlite3
import struct
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS documents (
 document_id TEXT PRIMARY KEY, title TEXT NOT NULL, area_name TEXT, parent_area_name TEXT,
 admin_level TEXT, fyp_name TEXT, plantype_label TEXT, library_fileext TEXT,
 content_source TEXT, source_layer TEXT NOT NULL DEFAULT 'private_upload',
 top_category TEXT NOT NULL DEFAULT '其他', subcategory TEXT, subsubcategory TEXT,
 source_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
 text TEXT NOT NULL, heading TEXT NOT NULL, heading_path TEXT NOT NULL, ordinal INTEGER NOT NULL,
 fragment INTEGER NOT NULL, embedding BLOB, embedding_model TEXT, UNIQUE(document_id,fragment)
);
CREATE INDEX IF NOT EXISTS idx_private_chunks_doc ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(terms);
CREATE TABLE IF NOT EXISTS index_state (
 id INTEGER PRIMARY KEY CHECK(id=1), model TEXT NOT NULL, dimensions INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1
);
"""


def terms(text: str) -> set[str]:
    output=set(re.findall(r"[a-z0-9][a-z0-9._-]*",text.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]+",text):
        output.add(run) if len(run)==1 else output.update(run[i:i+2] for i in range(len(run)-1))
    return output


def split_markdown(text: str, minimum=450, maximum=800, overlap=60):
    """Keep Markdown heading paths and paragraph boundaries when chunking."""
    stack=[]; paras=[]; buf=[]
    def flush():
        if buf:
            paras.append((" / ".join(stack) or "正文", " ".join(buf).strip())); buf.clear()
    for line in text.replace("\r","").splitlines():
        m=re.match(r"^(#{1,6})\s+(.+)$",line.strip())
        if m:
            flush(); level=len(m.group(1)); stack=stack[:level-1]+[m.group(2).strip()]
        elif line.strip(): buf.append(line.strip())
        else: flush()
    flush()
    out=[]; i=0
    while i<len(paras):
        heading, body=paras[i]; j=i+1
        while j<len(paras) and paras[j][0]==heading and len(body)<minimum and len(body)+len(paras[j][1])+1<=maximum:
            body += "\n"+paras[j][1]; j+=1
        start=0
        while start<len(body):
            end=min(start+maximum,len(body))
            if end<len(body):
                points=[body.rfind(ch,start+minimum//2,end) for ch in "。！？；\n"]
                point=max(points)
                if point>start: end=point+1
            part=body[start:end].strip()
            if part: out.append((heading, len(out)+1, part))
            if end>=len(body): break
            start=max(end-overlap,start+1)
        i=j
    return out


class KnowledgeStore:
    def __init__(self, path: str|Path):
        self.path=Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as con:
            con.executescript(SCHEMA)
            self._repair_fts(con)

    def connect(self):
        con=sqlite3.connect(self.path,timeout=10)
        con.row_factory=sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA busy_timeout=10000"); con.execute("PRAGMA journal_mode=WAL")
        return con

    @staticmethod
    def _repair_fts(con):
        # FTS rows are not foreign keys: repair stale/missing IDs explicitly.
        con.execute("BEGIN IMMEDIATE")
        indexed={r[0] for r in con.execute("SELECT rowid FROM chunks_fts")}
        actual={r[0] for r in con.execute("SELECT id FROM chunks")}
        if indexed != actual:
            con.execute("DELETE FROM chunks_fts")
            for row in con.execute("SELECT c.id,c.heading,c.text,d.title FROM chunks c JOIN documents d USING(document_id)"):
                con.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)",(row["id"]," ".join(sorted(terms(row["title"]+" "+row["heading"]+" "+row["text"])))))
        con.commit()

    @staticmethod
    def _validate_vectors(vectors, count):
        if len(vectors) != count:
            raise ValueError("embedding count mismatch")
        dims={len(v) for v in vectors}
        if count and (len(dims)!=1 or not next(iter(dims))):
            raise ValueError("embedding dimensions mismatch")
        for vector in vectors:
            if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in vector) or not any(vector):
                raise ValueError("invalid embedding vector")

    def reindex(self, model: str, embed):
        """Build all vectors off-transaction, switch atomically after validation.

        Previous vectors remain usable if embedding fails or documents change
        while the new vectors are being built. API credentials remain in the
        caller's request scope and are never recorded here.
        """
        if not model or not callable(embed):
            raise ValueError("model and embedding callback are required")
        with closing(self.connect()) as con:
            snapshot=[tuple(r) for r in con.execute("SELECT id,heading,text FROM chunks ORDER BY id")]
        vectors=embed([heading+"\n"+text for _,heading,text in snapshot]) if snapshot else []
        self._validate_vectors(vectors,len(snapshot))
        with closing(self.connect()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                current=[tuple(r) for r in con.execute("SELECT id,heading,text FROM chunks ORDER BY id")]
                if current != snapshot:
                    raise ValueError("knowledge changed during reindex; retry required")
                con.executemany("UPDATE chunks SET embedding=?,embedding_model=? WHERE id=?",[(struct.pack("<%sf"%len(vec),*vec),model,row[0]) for row,vec in zip(snapshot,vectors)])
                dims=len(vectors[0]) if vectors else 0
                con.execute("INSERT INTO index_state(id,model,dimensions,generation) VALUES(1,?,?,1) ON CONFLICT(id) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,generation=index_state.generation+1",(model,dims))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        return len(snapshot)

    def index_text(self, document_id: str, title: str, text: str, source_path: str, metadata: dict, embed):
        chunks=split_markdown(text)
        if not chunks:
            raise ValueError("no indexable text")
        now=metadata.get("now")
        if not now: raise ValueError("metadata.now is required")
        vectors=embed([f"{heading}\n{body}" for heading,_,body in chunks]) if embed and chunks else []
        if embed and chunks: self._validate_vectors(vectors,len(chunks))
        con=self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)",(document_id,))
            con.execute("DELETE FROM documents WHERE document_id=?",(document_id,))
            con.execute("INSERT INTO documents(document_id,title,area_name,parent_area_name,admin_level,fyp_name,plantype_label,library_fileext,content_source,source_layer,top_category,subcategory,subsubcategory,source_path,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (document_id,title,metadata.get("area_name"),metadata.get("parent_area_name"),metadata.get("admin_level"),metadata.get("fyp_name"),metadata.get("plantype_label"),metadata.get("file_ext"),metadata.get("content_source","user_upload"),metadata.get("source_layer","private_upload"),metadata.get("top_category","其他"),metadata.get("subcategory"),metadata.get("subsubcategory"),source_path,"ready",now,now))
            for i,(heading,ordinal,body) in enumerate(chunks):
                vec=struct.pack("<%sf"%len(vectors[i]),*vectors[i]) if vectors else None
                cur=con.execute("INSERT INTO chunks(document_id,text,heading,heading_path,ordinal,fragment,embedding,embedding_model) VALUES(?,?,?,?,?,?,?,?)",(document_id,body,heading,heading,ordinal,i+1,vec,metadata.get("embedding_model") if vec else None))
                con.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)",(cur.lastrowid," ".join(sorted(terms(title+" "+heading+" "+body)))))
            con.commit()
        except Exception:
            con.rollback(); raise
        finally: con.close()
        return len(chunks)

    def index_status(self, model=None, dimensions=None):
        """Describe compatibility without calling the provider or exposing keys."""
        with closing(self.connect()) as con:
            total=con.execute("SELECT count(*) FROM chunks").fetchone()[0]
            groups=[dict(r) for r in con.execute("SELECT embedding_model AS model,length(embedding)/4 AS dimensions,count(*) AS count FROM chunks WHERE embedding IS NOT NULL GROUP BY embedding_model,length(embedding)")]
            active=con.execute("SELECT model,dimensions,generation FROM index_state WHERE id=1").fetchone()
        embedded=sum(g['count'] for g in groups)
        complete=bool(total and embedded==total and len(groups)==1)
        compatible=complete and (model is None or groups[0]['model']==model) and (not dimensions or groups[0]['dimensions']==dimensions)
        warning=""
        if total and not complete: warning="向量索引不完整或混用了模型，请重建索引；当前使用关键词检索。"
        elif complete and not compatible: warning="查询模型或维度与索引不兼容，请选回原模型或完成索引重建；当前使用关键词检索。"
        return {"chunks":total,"embedded":embedded,"models":groups,"active":dict(active) if active else None,"compatible":compatible,"warning":warning}

    def list_documents(self):
        with closing(self.connect()) as con:
            return [dict(row)|{"chunks":con.execute("SELECT count(*) FROM chunks WHERE document_id=?",(row["document_id"],)).fetchone()[0],"embedded":con.execute("SELECT count(*) FROM chunks WHERE document_id=? AND embedding IS NOT NULL",(row["document_id"],)).fetchone()[0]} for row in con.execute("SELECT * FROM documents ORDER BY updated_at DESC")]

    def delete_document(self, document_id: str):
        with closing(self.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            ids=[r[0] for r in con.execute("SELECT id FROM chunks WHERE document_id=?",(document_id,))]
            con.executemany("DELETE FROM chunks_fts WHERE rowid=?",[(i,) for i in ids])
            cur=con.execute("DELETE FROM documents WHERE document_id=?",(document_id,)); con.commit()
            return cur.rowcount==1

    def search(self, query: str, embed=None, embedding_model: str|None=None, limit=8):
        qterms=sorted(terms(query))[:24]; lexical={}
        con=self.connect()
        try:
            if qterms:
                match=" OR ".join('"'+t.replace('"','')+'"' for t in qterms)
                lexical={r["rowid"]:i+1 for i,r in enumerate(con.execute("SELECT rowid,bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT 100",(match,)))}
            semantic={}
            total=con.execute("SELECT count(*) FROM chunks").fetchone()[0]
            complete=con.execute("SELECT count(*) FROM chunks WHERE embedding_model=? AND embedding IS NOT NULL",(embedding_model,)).fetchone()[0]
            if embed and total and complete==total:
                query_vectors=embed([query]); self._validate_vectors(query_vectors,1)
                qv=query_vectors[0]; qnorm=math.sqrt(sum(x*x for x in qv)) or 1
                rows=con.execute("SELECT c.*,d.title,d.source_path FROM chunks c JOIN documents d USING(document_id) WHERE c.embedding_model=? AND c.embedding IS NOT NULL",(embedding_model,))
                scores=[]
                for r in rows:
                    vec=struct.unpack("<%sf"%(len(r["embedding"])//4),r["embedding"])
                    if len(vec)!=len(qv): continue
                    norm=math.sqrt(sum(x*x for x in vec)) or 1
                    scores.append((sum(a*b for a,b in zip(qv,vec))/(qnorm*norm),r["id"]))
                semantic={ident:i+1 for i,(score,ident) in enumerate(sorted(scores,reverse=True)[:100])}
            fused={}
            for ranks in (lexical,semantic):
                for ident,rank in ranks.items(): fused[ident]=fused.get(ident,0)+1/(60+rank)
            ids=sorted(fused,key=fused.get,reverse=True)[:limit*8]
            found={}
            if ids:
                placeholders=",".join("?" for _ in ids)
                found={r["id"]:dict(r) for r in con.execute("SELECT c.*,d.title,d.source_path FROM chunks c JOIN documents d USING(document_id) WHERE c.id IN ("+placeholders+")",ids)}
            best={}
            for ident in ids:
                row=found.get(ident)
                if row: best.setdefault(row["document_id"],row | {"score":round(fused[ident],6)})
            result=list(best.values())[:limit]
            for row in result: row.pop("embedding",None); row.pop("embedding_model",None)
            return result
        finally: con.close()
