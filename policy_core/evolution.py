"""Evolution operations for one policy engine instance."""
from urllib.parse import quote


class EvolutionMixin:
    def evolution_answer(self, question, limit_period=2):
        """跨时期演变对比：同一地区、同一主题按规划期/年份排序，逐期给出原文与要点。

    所有要点都绑定一条逐字引文，引文必须能在对应原文里原样找到，否则丢弃；
    这样可以区分"规划原文说了什么"和"基于原文的概括"，避免编造政策演变。
    """
        con=self.db()
        try:
            meta=self._metadata_scope(con)
            norm=self.normalized_text(question)
            areas=[a for a in meta.get('areas',{}) if len(a)>=3 and self.normalized_text(a) in norm]
            if areas:
                areas=[max(areas,key=len)]
                marks=','.join('?' for _ in areas)
                docs=[dict(r) for r in con.execute(f"SELECT planid,title,area_name,fyp_name,source_name FROM documents WHERE status='ready' AND area_name IN ({marks})",areas)]
            else:
                # 问题里没点名地区时，从检索结果里挑一个"跨期覆盖最好"的地区，
                # 否则会随机落到只有单一规划期的地区，得出"无法对比"。
                seed=self.retrieve(question,limit=8)
                if not seed: return None, None
                candidates=list(dict.fromkeys(r['area_name'] for r in seed if r.get('area_name')))
                best=None
                for area in candidates:
                    periods=con.execute("SELECT count(DISTINCT fyp_name) FROM documents WHERE status='ready' AND area_name=?",(area,)).fetchone()[0]
                    docs_count=con.execute("SELECT count(*) FROM documents WHERE status='ready' AND area_name=?",(area,)).fetchone()[0]
                    if periods and periods>=2 and (best is None or (periods,docs_count)>(best[1],best[2])):
                        best=(area,periods,docs_count)
                if not best: return None, None
                areas=[best[0]]
                docs=[dict(r) for r in con.execute("SELECT planid,title,area_name,fyp_name,source_name FROM documents WHERE status='ready' AND area_name=?",(areas[0],))]
            if len(docs)<2: return None, None
            groups={}
            for d in docs:
                start,label=self.period_info(d['fyp_name'],d['title'])
                groups.setdefault((start,label),[]).append(d)
            periods=[]
            for (start,label),items in sorted(groups.items()):
                planids=[d['planid'] for d in items]
                rows=self.retrieve(question,limit=limit_period,planids=planids)
                if not rows: continue
                periods.append({'label':label,'start_year':start,'titles':[d['title'] for d in items][:6],
                                'doc_count':len(items),'rows':rows})
            if len(periods)<2: return None, None
        finally:
            con.close()
        # 证据编号在全局连续，便于前端与导出统一引用。
        sources=[]; seq=0
        for p in periods:
            p['source_ids']=[]; p['points']=[]
            for row in p['rows']:
                seq+=1; row['id']=seq
                sources.append(row); p['source_ids'].append(seq)
                p['points'].append({'text':self._clip(row['text'],150),'source_id':seq})
            p.pop('rows')
        model_lines=None
        if self.environment.getenv('LLM_API_KEY','').strip():
            model_lines=self._evolution_claims(question,periods,sources)
        lines=[]
        for p in periods:
            label=p['label'] if p['label']!='政府工作报告' else '政府工作报告'
            span=f'{p["start_year"]}年起' if p['start_year']<9998 else ''
            head=f'【{label}】{span}（覆盖 {p["doc_count"]} 份文件）'
            lines.append(head)
            claims=(model_lines or {}).get(p['label']) or []
            if claims:
                for c in claims: lines.append(f'· {c["point"]}（原文：“{c["quote"]}”）[{" ".join(f"[{i}]" for i in c["source_ids"])}]')
            else:
                for point in p['points']:
                    lines.append(f'· {point["text"]} [{point["source_id"]}]')
            lines.append('')
        status='evolution_verified' if model_lines else 'evolution_evidence'
        return '\n'.join(lines).strip(), {'periods':periods,'sources':sources,'status':status,'area':areas[0]}


    def _evolution_claims(self, question, periods, sources):
        """一次模型调用产出各期要点，逐条要求给出逐字引文并做一致性硬校验。"""
        index={s['id']:s for s in sources}
        blocks=[]
        for p in periods:
            evidence='\n'.join(f'[{i}] {sources[i-1]["text"][:400]}' for i in p['source_ids'])
            blocks.append(f'### {p["label"]}\n{evidence}')
        prompt=('你在做"政策演变"对照。下面按规划期分组给出证据原文，编号是全局引用号。'
          '对每个规划期，提炼不超过 3 条要点，说明该期在用户所问主题上的目标或举措。'
          '每条要点必须附一条 quote：quote 必须从该期证据原文中逐字复制，不得改写、不得跨期拼接。'
          '只输出 JSON：{"periods":[{"label":"规划期","points":[{"point":"一句话要点","quote":"逐字原文","source_ids":[1]}]}]}。'
          '证据不足的规划期可以给空数组，禁止推测。\n'
          f'问题：{question}\n\n'+'\n\n'.join(blocks))
        try:
            data=self.parse_model_json(self.model_call([{'role':'user','content':prompt}],0))
        except (RuntimeError,ValueError,TypeError):
            return None
        out={}
        for item in data.get('periods') or []:
            if not isinstance(item,dict): continue
            label=str(item.get('label') or '')
            kept=[]
            for point in item.get('points') or []:
                if not isinstance(point,dict): continue
                text=str(point.get('point') or '').strip()
                quote=str(point.get('quote') or '').strip()
                ids=point.get('source_ids')
                if not text or not quote or not isinstance(ids,list) or not ids: continue
                try: ids=list(dict.fromkeys(int(x) for x in ids))
                except (TypeError,ValueError): continue
                if any(i not in index for i in ids): continue
                haystack=self.normalized_text(''.join(index[i]['text'] for i in ids))
                if self.normalized_text(quote) not in haystack: continue     # 引文必须逐字命中
                kept.append({'point':text[:200],'quote':quote[:200],'source_ids':ids})
            if kept: out[label]=kept[:3]
        return out or None


