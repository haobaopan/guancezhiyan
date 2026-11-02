"""Answers operations for one policy engine instance."""
from concurrent.futures import ThreadPoolExecutor
import math
import re


class AnswersMixin:
    def existence_answer(self, question):
        """Search a resolved knowledge-base scope in full and only make bounded claims."""
        if not self.environment.getenv('LLM_API_KEY','').strip():
            return '未配置模型，无法可靠识别查找对象；本次不作存在或不存在判断。', [], 'inconclusive'
        con=self.db()
        docs=[dict(r) for r in con.execute('SELECT planid,title,source_name,area_name,admin_level,parent_area_name,fyp_name,split,status,text_len FROM documents ORDER BY planid')]
        if not docs:
            con.close()
            return '知识库中没有可检索文档，无法判断。', [], 'inconclusive'
        # Ask only for the search target and lexical variants. Scope is resolved below
        # against literal user-specified metadata, never accepted solely from the model.
        analyze=('从问题中提取要查找的具体事项，并给出少量可用于全文搜索的同义词/常见表述。'
          'target 必须是问题中明确提到的对象，不得补充推断；terms 必须包含 target。只输出 JSON：'
          '{"target":"查找对象","terms":["原词","同义表述"]}。\n问题：'+question)
        try:
            spec=self.parse_model_json(self.model_call([{'role':'user','content':analyze}],0))
            target=str(spec.get('target','')).strip()
            terms=spec.get('terms')
            if not target or not isinstance(terms,list) or not any(self.normalized_text(target)==self.normalized_text(x) for x in terms if isinstance(x,str)):
                raise ValueError('查找对象不明确')
            # Target must be visibly grounded in the user's own wording.
            if self.normalized_text(target) not in self.normalized_text(question): raise ValueError('查找对象无法从问题中直接定位')
            terms=list(dict.fromkeys([target]+[str(x).strip() for x in terms if isinstance(x,str) and 1<len(str(x).strip())<=40]))[:12]
            qnorm=self.normalized_text(question)
            explicit_files=[d for d in docs if any(len(str(v or ''))>=5 and self.normalized_text(v) in qnorm for v in (d['source_name'],d['title']))]
            years=set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',question))
            requested_regions=re.findall(r'[\u4e00-\u9fff]{2,8}(?:省|市|自治区|特别行政区)',question)
            regions=[d for d in docs if d.get('area_name') and self.normalized_text(d['area_name']) in qnorm]
            if explicit_files:
                scope_docs=explicit_files
                # If the question names a file-like string that cannot be resolved, fail closed.
                named=re.search(r'《([^》]+)》|([\w\u4e00-\u9fff-]+\.(?:md|txt|pdf|docx))',question,re.I)
                if named and not any(self.normalized_text(named.group(1) or named.group(2)) in self.normalized_text(d['source_name'] or d['title']) for d in explicit_files):
                    raise ValueError('指定文件未能唯一定位')
            elif re.search(r'《[^》]+》|[\w\u4e00-\u9fff-]+\.(?:md|txt|pdf|docx)',question,re.I):
                raise ValueError('指定文件未收录或无法定位')
            elif re.search(r'(?:某个?|该|这份|此)文件(?:中|里|内)',question) and not (regions or years):
                raise ValueError('问题指代了未能定位的具体文件')
            elif regions:
                scope_docs=regions
            elif requested_regions:
                raise ValueError('问题指定的地区未能在知识库中定位')
            elif years:
                scope_docs=[]
                for d in docs:
                    rows=con.execute('SELECT text FROM chunks WHERE planid=?',(d['planid'],)).fetchall()
                    alltext='\n'.join(r['text'] for r in rows)
                    if any(y in (str(d.get('title',''))+' '+alltext) for y in years): scope_docs.append(d)
            else:
                scope_docs=docs
            if not scope_docs: raise ValueError('问题指定的范围未能在知识库中定位')
            ids=[d['planid'] for d in scope_docs]
            placeholders=','.join('?' for _ in ids)
            chunk_rows=[dict(r) for r in con.execute(f'''SELECT c.*,d.title,d.source_name,d.area_name,d.admin_level,d.parent_area_name,d.fyp_name,d.split,d.source_path,d.status
            FROM chunks c JOIN documents d ON d.planid=c.planid WHERE c.planid IN ({placeholders}) ORDER BY d.planid,c.fragment''',ids)]
            # 同上：向量 blob 不能进 JSON（bytes 不可序列化），导出与缓存都用不到它。
            for r in chunk_rows:
                r.pop('embedding', None)
                r.pop('embedding_model', None)
            counts={r['planid']:0 for r in scope_docs}
            for r in chunk_rows: counts[r['planid']]=counts.get(r['planid'],0)+1
            complete=all(d.get('status')=='ready' and counts.get(d['planid'],0)>0 and int(d.get('text_len') or 0)>0 for d in scope_docs)
            if any(d.get('status')=='empty' for d in scope_docs): complete=False
            # Scan every indexed chunk in the resolved scope, not top-k retrieval results.
            hits=[]
            norm_terms=[(t,self.normalized_text(t)) for t in terms if self.normalized_text(t)]
            for row in chunk_rows:
                txt=self.normalized_text(row['text'])
                matched=[t for t,nt in norm_terms if nt in txt]
                if matched:
                    hits.append(row|{'matched_terms':matched})
            if self.embedding_enabled():
                qvec=self.embed_texts([question])[0]; qnorm=math.sqrt(sum(x*x for x in qvec)) or 1
                vector_hits=[]; vectorized=0
                for row in chunk_rows:
                    if not row.get('embedding') or row.get('embedding_model')!=self.EMBEDDING_MODEL: continue
                    vec=self.unpack_vector(row['embedding'])
                    if len(vec)!=len(qvec): continue
                    vectorized+=1; norm=math.sqrt(sum(x*x for x in vec)) or 1
                    score=sum(a*b for a,b in zip(qvec,vec))/(qnorm*norm)
                    if score>=float(self.environment.getenv('EXISTENCE_VECTOR_THRESHOLD','0.38')):
                        vector_hits.append((score,row))
                if vectorized < len(chunk_rows): complete=False
                have={r['id'] for r in hits}
                for score,row in sorted(vector_hits,key=lambda x:x[0],reverse=True)[:40]:
                    if row['id'] not in have: hits.append(row|{'matched_terms':[],'semantic_score':score}); have.add(row['id'])
            scope_label='、'.join(dict.fromkeys(str(d.get('source_name') or d.get('title')) for d in scope_docs[:8]))
            if len(scope_docs)>8: scope_label+=f'等{len(scope_docs)}份文档'
            # A direct lexical hit is only a candidate. A separate evidence check must
            # affirm that the original passage directly states the requested matter.
            def judge(row):
                check=('判断证据原文是否直接明确陈述查找对象存在。只有原文直接写出该措施/事项，且不是仅提到相近词、否定、计划建议或背景时，才判 supported。'
                  '只输出 JSON：{"verdict":"supported|contradicted|insufficient"}。'
                  f'\n查找对象：{target}\n原文：{row["text"]}')
                try: return row if self.parse_model_json(self.model_call([{'role':'user','content':check}],0)).get('verdict')=='supported' else None
                except RuntimeError: return None

            batch=hits[:40]
            verified=[]
            if batch:
                with ThreadPoolExecutor(max_workers=min(self.VERIFY_CONCURRENCY,len(batch))) as pool:
                    verified=[x for x in pool.map(judge,batch) if x]
            if verified:
                exposed=[]
                for i,row in enumerate(verified[:8],1):
                    exposed.append({'id':i,'planid':row['planid'],'filename':row.get('source_name') or row['title'],'title':row['title'],'area_name':row['area_name'],'fyp_name':row['fyp_name'],'heading':row['heading'],'ordinal':row['ordinal'],'fragment':row['fragment'],'text':row['text'],'score':1.0,'source_path':row['source_path'],'top_category':row['split'],'subcategory':row['admin_level'],'subsubcategory':row['fyp_name']})
                answer=f'找到关于“{target}”的直接证据，范围：{scope_label}。\n'+'\n'.join(f"[{s['id']}] {s['filename']}｜{s['heading']}：{s['text']}" for s in exposed)
                return answer,exposed,'existence_found'
            if hits:
                return f'检索到疑似相关内容，但未能确认其直接支持“{target}”存在；范围：{scope_label}。',[], 'inconclusive'
            if complete:
                return f'在当前知识库已收录且全文可检索的范围（{scope_label}）内，未发现查找对象“{target}”及其检索词：'+'、'.join(terms)+ '。这不代表知识库之外不存在。',[], 'not_found_in_complete_scope'
            return f'当前范围（{scope_label}）存在空文档或全文索引不完整，无法判断“{target}”是否存在。',[], 'inconclusive'
        except (ValueError,TypeError,KeyError,RuntimeError):
            return '查找对象或范围无法可靠确定，或自动校验未完成；无法判断。',[], 'inconclusive'
        finally:
            con.close()


    def model_answer(self, question, sources, mode='ask'):
        """Generate atomic claims, verify each against its cited passages, and fail closed."""
        if not self.environment.getenv('LLM_API_KEY','').strip(): return None, 'evidence_only'
        source_map={int(s['id']):s for s in sources}
        if mode=='compare' and len({s.get('planid') for s in sources})<2:
            return '本次检索没有覆盖至少两个不同文件，无法进行自动比较。请调整问题或扩大检索范围。', 'insufficient'
        refs='\n\n'.join(f"[{s['id']}] 文件：{s['filename']}；章节：{s['heading']}；段落：{s['ordinal']}；片段：{s['fragment']}\n{s['text']}" for s in sources)
        task={'ask':'回答用户问题','extract':'提取目标、任务、责任主体、时间节点、对象范围和保障措施；未明确的字段不要推断','compare':'按文件分别比较目标、对象、举措、指标和时间安排；不得把不同文件的内容合并归属'}[mode]
        coverage_prompt=('判断给定检索证据是否足以回答用户问题。比较任务必须覆盖至少两个不同文件；抽取任务须有直接相关片段。'
          '证据未覆盖问题核心、需要猜测、上下文不全或材料冲突无法区分时判 insufficient。只输出 JSON：'
          '{"verdict":"sufficient|insufficient","missing":"缺失点或冲突；足够则为空"}。'
          f'\n任务：{task}\n问题：{question}\n证据：\n{refs}')
        try:
            coverage=self.parse_model_json(self.model_call([{'role':'user','content':coverage_prompt}],0))
        except self.ModelUnavailable as exc:
            # 配置问题（欠费 / 密钥无效 / 无权限）：当作"没有模型"，返回证据列表而不是报错。
            self.log.warning('模型接口不可用，本次降级为证据模式：%s', exc)
            return None, 'evidence_only'
        if coverage.get('verdict')!='sufficient':
            missing=str(coverage.get('missing') or '检索证据未覆盖问题所需信息')[:300]
            return f'现有检索材料不足以自动回答：{missing}。', 'insufficient'
        prompt=(f'你是政策文本分析器。任务：{task}。只能使用证据原文。把答案拆成最小事实主张。'
          '每条主张必须带一个或多个来源编号；无证据项放入 unanswered。不要输出解释，只输出 JSON：'
          '{"claims":[{"text":"简短、单一事实主张","source_ids":[1],"claim_type":"fact/date/number/quote/paraphrase"}],"unanswered":["无法从材料确认的事项"]}。'
          f'问题：{question}\n\n证据：\n{refs}')
        raw=self.model_call([{'role':'user','content':prompt}],0)
        try:
            draft=self.parse_model_json(raw)
            claims=draft.get('claims')
            if not isinstance(claims,list) or not claims: raise ValueError('没有可校验的主张')
            candidates=[]
            for claim in claims[:40]:
                if not isinstance(claim,dict): continue
                text=str(claim.get('text','')).strip()
                ids=claim.get('source_ids')
                if not text or len(text)>800 or re.search(r'\[\s*\d+\s*\]',text) or not isinstance(ids,list) or not ids: continue
                try: ids=list(dict.fromkeys(int(x) for x in ids))
                except (TypeError,ValueError): continue
                if any(i not in source_map for i in ids): continue
                cited='\n'.join(source_map[i]['text'] for i in ids)
                # Deterministic hard check for dates, numbers, percentages, and quoted spans.
                critical=re.findall(r'(?:\d+(?:\.\d+)?%?|\d{4}年|第[一二三四五六七八九十百]+条)',text)
                if any(token not in cited for token in critical): continue
                if claim.get('claim_type')=='quote' and text not in cited: continue
                candidates.append((text,ids,cited))

            def verify(item):
                text,ids,cited=item
                check_prompt=('只依据给定原文审核主张。输出 JSON：{"verdict":"supported|contradicted|insufficient","reason":"简短理由"}。'
                  'supported 仅表示原文直接陈述或明确蕴含全部主张；推测、部分支持或上下文不足均为 insufficient。'
                  f'\n主张：{text}\n来源原文：\n{cited}')
                try: verdict=self.parse_model_json(self.model_call([{'role':'user','content':check_prompt}],0))
                except RuntimeError: return None
                if verdict.get('verdict')=='supported' and isinstance(verdict.get('reason'),str):
                    return {'text':text,'source_ids':ids}
                return None

            safe=[]
            if candidates:
                # Serial verification took one round-trip per claim and blew the 180s
                # budget; run them in parallel but keep the original claim order.
                with ThreadPoolExecutor(max_workers=min(self.VERIFY_CONCURRENCY,len(candidates))) as pool:
                    safe=[x for x in pool.map(verify,candidates) if x]
            if not safe:
                return '现有检索材料不足以自动确认可回答的事实主张。\n\n检索到的原文如下，请据此调整问题或补充材料。\n'+ '\n'.join(f"[{s['id']}] {s['filename']}｜{s['heading']}：{s['text']}" for s in sources[:5]), 'insufficient'
            # Compose deterministically from verified claims, so final wording cannot add new facts.
            lines=[]
            for c in safe:
                cites=' '.join(f"[{i}]" for i in c['source_ids'])
                lines.append(f"{c['text']} {cites}")
            rejected=len(claims)-len(safe)
            if rejected: lines.append('部分主张未通过自动证据校验，已省略。')
            return '\n'.join(lines), ('partial' if rejected else 'verified')
        except (ValueError,TypeError,KeyError,RuntimeError):
            # Do not return unvalidated model text. Keep only source material and a clear status.
            return '自动一致性校验未能完成，未生成综合结论。以下仅为本次检索到的原文证据：\n'+'\n'.join(f"[{s['id']}] {s['filename']}｜{s['heading']}：{s['text']}" for s in sources[:5]), 'validation_failed'


    def sources_payload(self, rows):
        return [{'id':i,'planid':r['planid'],'filename':r['source_name'] or r['title'] or r['filename'],'title':r['title'],'area_name':r['area_name'],'fyp_name':r['fyp_name'],'heading':r['heading'],'ordinal':r['ordinal'],'fragment':r['fragment'],'text':r['text'],'score':r['score'],'source_path':r['source_path'],'top_category':r['top_category'],'subcategory':r['subcategory'],'subsubcategory':r['subsubcategory']} for i,r in enumerate(rows,1)]


