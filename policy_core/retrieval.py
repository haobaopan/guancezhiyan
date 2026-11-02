"""Retrieval operations for one policy engine instance."""
import math
import re
import sqlite3
import time


class RetrievalMixin:
    def _warn_semantic(self, msg):
        """同一条语义降级告警每分钟最多一次，避免一个挂掉的端点刷满日志。"""
        now=time.time()
        if msg==self._SEMANTIC_WARN['msg'] and now-self._SEMANTIC_WARN['at']<60: return
        self._SEMANTIC_WARN.update(at=now,msg=msg)
        self.log.warning(msg)


    def _roman_alias(self, fyp_name):
        """'第十四个五年规划' → '十四五'，用于把口语化的规划期映射到元数据。"""
        m = re.search(r'第([一二三四五六七八九十]+)个?五?年?(?:规划|计划)', fyp_name or '')
        if not m:
            return ''
        num = m.group(1)
        return '十五' if num == '十' else num + '五'


    def _metadata_scope(self, con):
        """按行政区和规划期建立 planid 索引；文档数变化时自动刷新。"""
        count = con.execute('SELECT count(*) FROM documents').fetchone()[0]
        if self._SCOPE_CACHE.get('count') == count:
            return self._SCOPE_CACHE
        areas, fyps = {}, {}
        for planid, area, fyp in con.execute(
                "SELECT planid,area_name,fyp_name FROM documents WHERE status='ready'"):
            if area:
                areas.setdefault(str(area), set()).add(planid)
            if fyp:
                fyps.setdefault(str(fyp), set()).add(planid)
        self._SCOPE_CACHE.update({'count': count, 'areas': areas, 'fyps': fyps})
        return self._SCOPE_CACHE


    def _scope_planids(self, con, query):
        """从问题里识别“哪个地区 + 哪个规划期”，返回候选 planid 集合。

    政策问题几乎都会带地名和规划期，而这两项正好是库里已有的元数据；
    不利用它们，正确的文件会被大量同主题文本淹没。
    """
        try:
            meta = self._metadata_scope(con)
        except sqlite3.DatabaseError:
            return None
        norm = self.normalized_text(query)
        if not norm:
            return None
        areas = [a for a in meta.get('areas', {}) if len(a) >= 3 and self.normalized_text(a) in norm]
        if areas:
            longest = max(len(a) for a in areas)
            areas = [a for a in areas if len(a) == longest]          # 只接受最具体的地名
        fyps = []
        for fyp in meta.get('fyps', {}):
            alias = self._roman_alias(fyp)
            if alias and alias in query:
                fyps.append(fyp)
        scope = None
        if areas:
            scope = set().union(*(meta['areas'][a] for a in areas))
        if fyps:
            fset = set().union(*(meta['fyps'][f] for f in fyps))
            scope = fset if scope is None else (scope & fset)
        return scope or None


    def retrieve(self, query, limit=8, planids=None):
        """Fuse full-text BM25 and cosine semantic rankings using RRF.

    planids 给定时只在指定文档集合内排序（跨时期演变按规划期逐期检索要用）。
    """
        con=self.db(); select="SELECT c.*, d.title,d.source_name,d.area_name,d.admin_level,d.parent_area_name,d.fyp_name,d.plantype_label,d.split,d.top_category,d.subcategory,d.subsubcategory,d.source_path,d.status FROM chunks c JOIN documents d ON d.planid=c.planid WHERE d.status='ready'"
        if planids is not None:
            planids=set(planids)
            if not planids:
                con.close(); return []
            marks=','.join('?' for _ in planids)
            select+=f' AND c.planid IN ({marks})'
            fixed=list(planids)
        else:
            fixed=None
        scope=self._scope_planids(con, query)
        pool=400 if scope else 100
        lexical={}
        try:
            terms=sorted(self.tokens(query))[:24]
            if terms:
                match=' OR '.join('"'+x.replace('"','')+'"' for x in terms)
                if fixed is None:
                    rows=con.execute('SELECT rowid,bm25(chunks_fts) AS rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?',(match,pool)).fetchall()
                else:
                    rows=con.execute(f'SELECT rowid,bm25(chunks_fts) AS rank FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN (SELECT id FROM chunks WHERE planid IN ({marks})) ORDER BY rank LIMIT ?',[match]+fixed+[pool]).fetchall()
                lexical={r['rowid']:i+1 for i,r in enumerate(rows)}
        except sqlite3.OperationalError as exc:
            self.log.warning('全文索引不可用，退回 Python 关键词比对（大数据量下会明显变慢）：%s', exc)
            q=self.tokens(query); ranked=[]
            for r in con.execute(select,fixed or ()):
                overlap=len(q & self.tokens(r['heading']+' '+r['title']+' '+r['text']))
                if overlap: ranked.append((overlap,r['id']))
            lexical={ident:i+1 for i,(_,ident) in enumerate(sorted(ranked,reverse=True)[:100])}
        semantic={}
        if self.embedding_enabled():
            try:
                total=con.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.planid=c.planid WHERE d.status='ready'").fetchone()[0]
                embedded=con.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.planid=c.planid WHERE d.status='ready' AND c.embedding IS NOT NULL AND c.embedding_model=?",(self.EMBEDDING_MODEL,)).fetchone()[0]
                # A partial vector corpus biases cosine ranking toward whichever documents
                # happened to be indexed first. Use semantic ranking only after full coverage.
                if total and embedded==total:
                    qvec=self.embed_texts([query])[0]; qnorm=math.sqrt(sum(x*x for x in qvec)) or 1; scored=[]
                    skipped=0
                    for r in con.execute(select+' AND c.embedding IS NOT NULL AND c.embedding_model=?',(fixed or [])+[self.EMBEDDING_MODEL]):
                        vec=self.unpack_vector(r['embedding'])
                        if len(vec)!=len(qvec):
                            skipped+=1; continue
                        norm=math.sqrt(sum(x*x for x in vec)) or 1
                        scored.append((sum(a*b for a,b in zip(qvec,vec))/(qnorm*norm),r['id']))
                    if skipped and not scored:
                        # 维度对不上说明库里的向量来自别的模型/端点，静默退回纯词法会让人以为语义召回生效了。
                        self._warn_semantic(f'库内向量维度与当前端点不一致（跳过了 {skipped} 条），本次只用关键词召回；'
                                       f'请重跑 构建示例知识库.py --index-embeddings')
                    semantic={ident:i+1 for i,(score,ident) in enumerate(sorted(scored,reverse=True)[:100])}
            except RuntimeError as e:
                self._warn_semantic(f'语义召回不可用，本次只用关键词召回：{e}')
                semantic={}
        fused={}
        for ident,rank in lexical.items(): fused[ident]=fused.get(ident,0)+1/(60+rank)
        for ident,rank in semantic.items(): fused[ident]=fused.get(ident,0)+1/(60+rank)
        if not fused: con.close(); return []
        # Pull a wide candidate pool, narrow it to the asked-for region/period, then
        # keep only the best passage per document so eight results cover eight files.
        ids=sorted(fused,key=fused.get,reverse=True)[:max(pool,limit*8)]
        marks=','.join('?' for _ in ids)
        found={r['id']:dict(r) for r in con.execute(select+f' AND c.id IN ({marks})',(fixed or [])+ids)}
        con.close()
        # 证据行只用于展示与引用，向量 blob 必须丢掉：
        # ① bytes 不能 JSON 序列化——零向量时代 embedding 是 NULL 所以没暴露，
        #    回填后 /api/evolution 会整条 500（2026-10-27 实测）；② 每条 4KB，8 条就多 32KB；
        # ③ 也没必要把向量下发给前端。
        for row in found.values():
            row.pop('embedding', None)
            row.pop('embedding_model', None)
        ordered=[found[i]|{'score':round(fused[i],6)} for i in ids if i in found]
        if scope:
            # Documents matching the asked-for region/period go first, but the
            # remaining slots are still filled so that comparison tasks keep at
            # least two documents and answers are not starved of evidence.
            narrowed=[row for row in ordered if row['planid'] in scope]
            if narrowed: ordered=narrowed+[row for row in ordered if row['planid'] not in scope]
        best={}
        for row in ordered:
            best.setdefault(row['planid'],row)
        return list(best.values())[:limit]


