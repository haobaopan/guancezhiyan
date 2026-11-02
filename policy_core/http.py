"""Legacy HTTP routes, authentication guard, uploads and downloads."""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlparse
import hashlib
import json
import re
import secrets
import sqlite3
import time
from .assets import WEB


def parse_multipart(engine, handler):
    """Parse multipart/form-data without cgi (removed from Python 3.13)."""
    ctype=handler.headers.get('Content-Type','')
    if 'multipart/form-data' not in ctype.lower(): raise ValueError('上传请求格式不正确（需要 multipart/form-data）。')
    try: length=int(handler.headers.get('Content-Length') or 0)
    except ValueError: length=0
    if length<=0: raise ValueError('上传内容为空。')
    if length>engine.MAX_UPLOAD*20: raise ValueError('单次上传总大小超出限制。')
    boundary=None
    for part in ctype.split(';'):
        part=part.strip()
        if part.lower().startswith('boundary='):
            boundary=part[9:].strip().strip('"')
    if not boundary: raise ValueError('上传请求缺少分隔符。')
    raw=handler.rfile.read(length)
    delimiter=b'--'+boundary.encode('latin-1','replace')
    items=[]
    for segment in raw.split(delimiter):
        segment=segment.strip(b'\r\n')
        if not segment or segment==b'--': continue
        if b'Content-Disposition' not in segment[:400]: continue
        head,sep,body=segment.partition(b'\r\n\r\n')
        if not sep: head,sep,body=segment.partition(b'\n\n')
        headers=head.decode('utf-8','replace')
        field=re.search(r'name="([^"]*)"',headers)
        filename=re.search(r'filename\*?="?([^";\r\n]*)"?',headers)
        if not filename: continue
        if field and field.group(1)!='files': continue
        name=Path(filename.group(1).replace('\\','/')).name
        if not name or name in ('.','..'): continue
        if body.endswith(b'\r\n'): body=body[:-2]
        if len(body)>engine.MAX_UPLOAD: raise ValueError(f'{name} 超过20MB。')
        items.append((name,body))
    return items


class Handler(BaseHTTPRequestHandler):
    engine = None  # bound by make_handler(); never a process-global singleton
    server_version='GuanCeZhiYan/1.1'
    def guard(self):
        """限流 + 可选访问令牌。

        `/healthz` 在 do_GET 里提前返回，不经过这里，所以探活不需要令牌；
        其余路径（含首页 `/` 与 `/source/`）都受令牌保护。
        首页单独处理：浏览器直接打开时给一个"输入令牌"的页面，而不是一段 JSON，
        否则部署到校内服务器后，用户看到的是 {"error":"未授权…"}，不知道下一步该做什么。
        """
        client=self.client_address[0] if self.client_address else '-'
        # 经隧道（cloudflared）转发时，所有请求来源都是本机回环，限流会退化成"全员共用一个桶"，
        # 五个人同时用就会互相误伤。此时改取隧道带来的真实访客地址，按人计数。
        if client in ('127.0.0.1','::1','::ffff:127.0.0.1'):
            _fwd=(self.headers.get('CF-Connecting-IP') or '').strip()
            if _fwd:
                client='cf:'+_fwd
        if self.engine.RATE_LIMIT_PER_MIN:
            now=time.time()
            with self.engine._RATE_LOCK:
                bucket=[t for t in self.engine._RATE.get(client,[]) if now-t<60]
                if len(bucket)>=self.engine.RATE_LIMIT_PER_MIN:
                    self.send_json({'error':'请求过于频繁，请稍后再试。'},429); return False
                bucket.append(now); self.engine._RATE[client]=bucket
        if self.engine.ACCESS_TOKEN:
            _q_token=(parse_qs(urlparse(self.path).query).get('token') or [''])[0]
            supplied=(self.headers.get('X-Access-Token') or _q_token)
            if not supplied:
                # 扫码进来第一次带 ?token=，之后一律走 Cookie。
                # 这样地址栏才不会一直挂着 ?token=XXXX 那一长串口令。
                _m=re.search(r'(?:^|;\s*)gczy_token=([^;]+)', self.headers.get('Cookie') or '')
                if _m: supplied=unquote(_m.group(1))
            if not secrets.compare_digest(str(supplied),self.engine.ACCESS_TOKEN):
                if self.command=='GET' and urlparse(self.path).path=='/':
                    self.send_html(self.engine.TOKEN_GATE, 401); return False
                self.send_json({'error':'未授权：请提供有效的访问令牌。'},401); return False
            if _q_token and self.command=='GET':
                # 令牌已验过：收进 Cookie（HttpOnly，30 天），再 302 到不带令牌的干净地址。
                # 只跳 GET；POST 与 /healthz 不受影响。
                _u=urlparse(self.path)
                _keep={k:v[0] for k,v in parse_qs(_u.query).items() if k!='token'}
                _clean=_u.path+(('?'+urlencode(_keep)) if _keep else '')
                self.send_response(302)
                self.send_header('Location', _clean)
                self.send_header('Set-Cookie',
                    'gczy_token=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax' % self.engine.ACCESS_TOKEN)
                # 再种一枚非 HttpOnly 的副 cookie：地址栏干净之后，页面脚本仍要能拿到令牌，
                # 否则「复制引用」生成的链接不带 token，发给别人打不开。
                self.send_header('Set-Cookie',
                    'gczy_js=%s; Path=/; Max-Age=2592000; SameSite=Lax' % self.engine.ACCESS_TOKEN)
                self.send_header('Content-Length','0')
                self.end_headers()
                return False
        return True
    def send_json(self, obj, status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def send_html(self, html, status=200):
        raw=html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Security-Policy',
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'")
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def send_file(self, raw, name, mime):
        self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+__import__('urllib.parse').parse.quote(name)); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/healthz':
            # 探活：不要求令牌，只回聚合指标，不泄漏文件明细。
            try:
                con=self.engine.db()
                ready=con.execute("SELECT count(*) FROM documents WHERE status='ready'").fetchone()[0]
                total=con.execute('SELECT count(*) FROM chunks').fetchone()[0]
                indexed=con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
                embedded=con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND embedding_model=?',
                                     (self.engine.EMBEDDING_MODEL,)).fetchone()[0]
                stale=con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND '
                                  "COALESCE(embedding_model,'')<>?", (self.engine.EMBEDDING_MODEL,)).fetchone()[0]
                ready_chunks=con.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.planid=c.planid "
                                         "WHERE d.status='ready'").fetchone()[0]
                row=con.execute('SELECT length(embedding) FROM chunks WHERE embedding IS NOT NULL '
                                'AND embedding_model=? LIMIT 1', (self.engine.EMBEDDING_MODEL,)).fetchone()
                emb_dim=(row[0]//4) if row else 0
                con.close()
                ok=indexed==total
                semantic=bool(self.engine.embedding_enabled() and ready_chunks and embedded>=ready_chunks)
                self.send_json({'status':'ok' if ok else 'degraded','documents':ready,'chunks':total,
                                'fts_indexed':indexed,'embedded':embedded,'embedded_stale':stale,
                                'ready_chunks':ready_chunks,'embedding_model':self.engine.EMBEDDING_MODEL,
                                'embedding_dim':emb_dim,'embedding_keyless':self.engine.embedding_keyless(),
                                'semantic_active':semantic,'fts_available':self.engine.FTS_AVAILABLE,
                                'llm':bool(self.engine.environment.getenv('LLM_API_KEY','').strip()),'embedding':self.engine.embedding_enabled()},
                               200 if ok else 503)
            except sqlite3.DatabaseError:
                self.send_json({'status':'error','detail':'知识库不可读'},503)
            return
        if not self.guard(): return
        if path=='/':
            raw=self.engine.INDEX.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        elif path in ('/assets/style.css', '/assets/app.js'):
            name = path.rsplit('/', 1)[-1]
            raw = (WEB / name).read_bytes()
            mime = 'text/css' if name.endswith('.css') else 'text/javascript'
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path=='/api/files':
            con=self.engine.db(); rows=con.execute('''SELECT COALESCE(NULLIF(d.source_name,''),c.filename) AS filename, COUNT(*) AS chunks,
                d.title, d.area_name, d.admin_level, d.fyp_name, d.plantype_label,
                d.split, d.top_category, d.subcategory, d.subsubcategory, d.status
                FROM chunks c LEFT JOIN documents d ON d.planid=c.planid
                GROUP BY c.planid,c.filename ORDER BY filename''').fetchall(); con.close()
            files=[]
            for x in rows:
                item=dict(x)
                # Keep the old display keys while sourcing values from normalized metadata.
                item.update({'year': (re.search(r'(?:19|20)\d{2}', item.get('title') or '') or [None])[0] or '年份未标注',
                             'region': item.get('area_name') or '区域未标注',
                             'kind': item.get('plantype_label') or '其他/未识别'})
                files.append(item)
            self.send_json({'files':files})
        elif path.startswith('/source/'):
            planid=unquote(path[len('/source/'):])
            query=parse_qs(urlparse(self.path).query)
            try: frag=int((query.get('frag') or [''])[0])
            except (TypeError,ValueError): frag=None
            want_raw=(query.get('dl') or [''])[0]=='1'
            con=self.engine.db()
            row=con.execute('SELECT planid,source_path,title,library_fileext,source_name,split,status FROM documents WHERE planid=?',(planid,)).fetchone()
            chunk=None
            if row and frag is not None:
                chunk=con.execute('SELECT heading,ordinal,fragment,text FROM chunks WHERE planid=? AND fragment=?',(planid,frag)).fetchone()
            if row and chunk is None:
                chunk=con.execute('SELECT heading,ordinal,fragment,text FROM chunks WHERE planid=? ORDER BY fragment LIMIT 1',(planid,)).fetchone()
            con.close()
            if not row: self.send_json({'error':'原文件记录不存在'},404); return
            if row['status']=='empty' and chunk is None:
                self.send_html(self.engine.preview_page(row,None,'该文档在原库中为空正文（无可检索内容），仅登记元数据，本次不做任何推断。'))
                return
            relative=Path(row['source_path'])
            # Resolve safe relative paths in the unified store. Older databases
            # may still point to DOCS/<name> or the former dataset directory;
            # migrate those originals on first access.
            target=(self.engine.DOCS / relative).resolve() if not relative.is_absolute() else None
            if target is None or not target.is_relative_to(self.engine.DOCS.resolve()):
                target=None
            if target is None or not target.is_file():
                basename=Path(row['source_name'] or relative.name).name
                legacy_candidates=[]
                is_sample=row['split'] in ('corpus','edge_cases')
                if is_sample and not relative.is_absolute():
                    dataset_candidate=(self.engine.DATASET_ROOT/relative).resolve()
                    if dataset_candidate.is_relative_to(self.engine.DATASET_ROOT.resolve()): legacy_candidates.append(dataset_candidate)
                elif not is_sample:
                    legacy_candidates.append(self.engine.DOCS/basename)
                legacy=next((p for p in legacy_candidates if p.is_file()),None)
                # Some older example indexes saved only a basename even when the
                # corpus file lives in a subdirectory. Accept recursive recovery
                # only when the basename has exactly one match; never guess when
                # same-name source files are ambiguous.
                if legacy is None and is_sample and self.engine.DATASET_ROOT.is_dir():
                    matches=list(self.engine.DATASET_ROOT.rglob(basename))
                    if len(matches)==1: legacy=matches[0]
                    elif len(matches)>1:
                        self.send_json({'error':'示例库中存在多个同名原文件，无法安全确定来源；请重新建立索引并记录原文件路径'},409); return
                if legacy is None:
                    if is_sample:
                        self.send_json({'error':f'示例原文未找到：{relative.as_posix()}。请设置 POLICY_DATASET_DIR 指向包含 corpus/ 和 edge_cases/ 的数据集根目录。'},404)
                    else:
                        self.send_json({'error':'原文件不存在，请重新导入该文件'},404)
                    return
                if is_sample:
                    # 示例库文档就地预览：全库原件 55–77GB，不能逐份复制进本地存储。
                    target=legacy
                else:
                    raw=legacy.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
                    relative=Path('originals')/digest[:2]/f'{digest}{Path(basename).suffix[:20]}'
                    target=(self.engine.DOCS/relative).resolve(); self.engine.ensure_dir(target.parent)
                    if not target.exists(): target.write_bytes(raw)
                    con=self.engine.db(); con.execute('UPDATE documents SET source_path=?,source_name=CASE WHEN source_name=\'\' THEN ? ELSE source_name END WHERE planid=?',(relative.as_posix(),basename,planid)); con.commit(); con.close()
            display=row['source_name'] or (Path(row['title']).name+(target.suffix or ''))
            if want_raw:
                self.send_file(target.read_bytes(),display,'application/octet-stream'); return
            # Serving the original bytes as a download made "点击回看原文" useless:
            # browsers cannot render docx and the fragment was never located.
            kind,preview=self.engine.find_preview_source(row,target)
            needle=(chunk['text'] if chunk is not None else '') or ''
            if kind=='html':
                self.send_html(self.engine.with_highlight(preview.read_text(encoding='utf-8',errors='replace'),row,chunk,needle,display))
            elif kind=='md':
                self.send_html(self.engine.preview_page(row,chunk,body=self.engine.md_to_html(preview.read_text(encoding='utf-8',errors='replace')),
                                            needle=needle,filename=display))
            else:
                self.send_file(target.read_bytes(),display,'application/octet-stream')
        else:self.send_json({'error':'Not found'},404)
    def do_POST(self):
        path=urlparse(self.path).path
        if not self.guard(): return
        try:
            if path=='/api/upload':
                self.engine.ensure_dir(self.engine.DOCS); self.engine.ensure_dir(self.engine.SOURCE_STORE); results=[]; indexed=[]
                for name,data in parse_multipart(self.engine, self):
                    if len(data)>self.engine.MAX_UPLOAD: raise ValueError(f'{name} 超过20MB。')
                    info=self.engine.index_file(name,data); results.append(f'{name}（{info["chunks"]}段）'); indexed.append(info)
                if not results: raise ValueError('请选择文件。')
                self.send_json({'message':'已建索引：'+'；'.join(results),'documents':indexed})
            elif path=='/api/evolution':
                self.engine.set_deadline()
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0') or 0)) or b'{}')
                question=str(body.get('question','')).strip()
                if not question: raise ValueError('请先输入问题。')
                query,ctx=self.engine.resolve_followup(str(body.get('session_id') or ''),question)
                answer,payload=self.engine.evolution_answer(query)
                if not payload:
                    self.send_json({'answer':'没有找到可跨期对比的材料：该问题在知识库中只覆盖单一规划期，或缺少可识别的地区。请指定地区（如“××市”）或换一个主题再试。',
                                    'sources':[],'periods':[],'status':'no_evolution','question':question,'model':False})
                    return
                self.engine.remember_session(str(body.get('session_id') or ''),query,payload['sources'][:1])
                result_id=secrets.token_urlsafe(24)
                result={'answer':answer,'sources':payload['sources'],'periods':payload['periods'],
                        'question':question,'status':payload['status'],'area':payload['area']}
                with self.engine.RESULTS_LOCK:
                    if len(self.engine.RESULTS)>=self.engine.MAX_SAVED_RESULTS: self.engine.RESULTS.pop(next(iter(self.engine.RESULTS)))
                    self.engine.RESULTS[result_id]=result
                self.send_json(result|{'export_id':result_id,'model':bool(self.engine.environment.getenv('LLM_API_KEY','').strip()),
                                       'rewritten_query':query if query!=question else ''})
            elif path in ('/api/ask','/api/extract','/api/compare'):
                self.engine.set_deadline()
                length=int(self.headers.get('Content-Length','0'))
                if length>100000: raise ValueError('请求内容过大。')
                body=json.loads(self.rfile.read(length)); question=str(body.get('question','')).strip()
                if not question: raise ValueError('请先输入问题。')
                session_id=str(body.get('session_id') or '')
                mode='extract' if path.endswith('extract') else ('compare' if path.endswith('compare') else 'ask')
                query,ctx=self.engine.resolve_followup(session_id,question)
                if self.engine.is_existence_question(query):
                    answer,refs,status=self.engine.existence_answer(query)
                    result_id=secrets.token_urlsafe(24)
                    result={'answer':answer,'sources':refs,'question':question,'status':status}
                    with self.engine.RESULTS_LOCK:
                        if len(self.engine.RESULTS)>=self.engine.MAX_SAVED_RESULTS: self.engine.RESULTS.pop(next(iter(self.engine.RESULTS)))
                        self.engine.RESULTS[result_id]=result
                    self.send_json(result|{'export_id':result_id,'model':bool(self.engine.environment.getenv('LLM_API_KEY','').strip())})
                    return
                rows=self.engine.retrieve(query)
                if not rows: self.send_json({'answer':'知识库中没有找到相关内容。请先导入文件，或换一种表达。','sources':[],'model':False,'status':'no_evidence'}); return
                refs=self.engine.sources_payload(rows)
                self.engine.remember_session(session_id,query,rows)
                table=None
                if mode=='extract':
                    # 四要素抽取：模型给出要点+逐字引文，引文必须命中原文；模型不可用时退回规则整句抽取。
                    answer,table,status=self.engine.extract_answer(query,refs)
                else:
                    try: answer,status=self.engine.model_answer(query,refs,mode)
                    except self.engine.ModelUnavailable as e:
                        # 余额不足 / 密钥失效：不返回 502，降级为证据模式并说明原因
                        self.engine.log.warning('模型不可用，降级为证据模式：%s',e)
                        answer,status=None,'evidence_only'
                    except RuntimeError as e: self.send_json({'error':str(e)},502); return
                    if answer is None:
                        why=('生成模型不可用（密钥无效或余额不足），' if self.engine.environment.getenv('LLM_API_KEY','').strip()
                             else '未配置生成模型，')
                        answer=why+'当前仅返回检索证据，不生成未经校验的综合结论。\n'+'\n'.join(f"[{s['id']}] {s['filename']}｜{s['heading']}｜段落 {s['ordinal']}｜片段 {s['fragment']}：{s['text']}" for s in refs[:5])
                result_id=secrets.token_urlsafe(24)
                result={'answer':answer,'sources':refs,'question':question,'status':status,'table':table}
                with self.engine.RESULTS_LOCK:
                    if len(self.engine.RESULTS)>=self.engine.MAX_SAVED_RESULTS: self.engine.RESULTS.pop(next(iter(self.engine.RESULTS)))
                    self.engine.RESULTS[result_id]=result
                self.send_json(result|{'export_id':result_id,'model':status not in ('evidence_only','no_evidence','elements_rule'),
                                       'rewritten_query':query if query!=question else ''})
            elif path in ('/api/export/docx','/api/export/csv','/api/export/xlsx','/api/export/pptx'):
                length=int(self.headers.get('Content-Length','0'))
                if length>2_000_000: raise ValueError('导出内容过大。')
                data=json.loads(self.rfile.read(length)); result_id=str(data.get('export_id',''))
                with self.engine.RESULTS_LOCK: saved=self.engine.RESULTS.get(result_id)
                if not saved: self.send_json({'error':'分析结果已过期或无效，请重新检索生成。'},409); return
                answer=saved['answer']; sources=saved['sources']; table=saved.get('table') or None
                periods=saved.get('periods') or None
                if not answer: raise ValueError('没有可导出的分析结果。')
                title=str(saved.get('question') or '政策分析结果')[:180]
                fmt=path.rsplit('/',1)[-1]
                base=f'http://{self.headers.get("Host") or (self.engine.HOST+":"+str(self.engine.PORT))}'
                if fmt=='docx':
                    raw=self.engine.make_docx(title,answer,sources,table=table,base_url=base); mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                elif fmt=='pptx':
                    raw=self.engine.make_pptx(title,answer,sources,periods=periods); mime='application/vnd.openxmlformats-officedocument.presentationml.presentation'
                elif fmt=='xlsx':
                    header=['维度','要素表述','出处文件','章节','片段编号','原文引文']
                    body_rows=[[r['dim'],r['text'],r['filename'],r['heading'],r['fragment'],r.get('quote') or r['text']] for r in (table or [])]
                    if not body_rows:
                        header=['编号','文件名','章节','段落编号','片段编号','检索原文']
                        body_rows=[[s.get('id',''),s.get('filename',''),s.get('heading',''),s.get('ordinal',''),s.get('fragment',''),s.get('text','')] for s in sources]
                    raw=self.engine.make_xlsx(header,body_rows); mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                else:
                    import io,csv
                    out=io.StringIO(); w=csv.writer(out); w.writerow(['问题','分析结果','状态']); w.writerow([title,answer,saved.get('status','')]); w.writerow([])
                    if table:
                        w.writerow(['维度','要素表述','出处文件','章节','片段编号','原文引文'])
                        for r in table: w.writerow([r['dim'],r['text'],r['filename'],r['heading'],r['fragment'],r.get('quote') or r['text']])
                        w.writerow([])
                    w.writerow(['编号','文件名','章节','段落编号','片段编号','检索原文'])
                    for s in sources: w.writerow([s.get('id',''),s.get('filename',''),s.get('heading',''),s.get('ordinal',''),s.get('fragment',''),s.get('text','')])
                    raw=out.getvalue().encode('utf-8-sig'); mime='text/csv; charset=utf-8'
                self.send_file(raw,'管策智研分析结果.'+fmt,mime)
            else:self.send_json({'error':'Not found'},404)
        except ValueError as e:
            self.send_json({'error':str(e)},400)
        except Exception:
            # Never echo internal exception text (paths, SQL) back to the browser.
            self.engine.log.exception('请求处理失败：%s %s', self.command, self.path)
            self.send_json({'error':'服务处理失败，请查看终端日志或稍后重试。'},500)
    def log_message(self, fmt,*args):
        line=fmt%args
        print('%s - %s'%(self.address_string(),line))
        # 每次请求都落一行审计，便于事后核对"谁在什么时候问了什么、结果如何"。
        if self.engine.AUDIT_LOG is not None:
            self.engine.audit({'ts':time.strftime('%Y-%m-%dT%H:%M:%S'),'ip':self.address_string(),
                   'method':getattr(self,'command',''),'path':self.path,'detail':line})


def make_handler(engine):
    """Bind each server to its own engine without changing global handler state."""
    class BoundHandler(Handler):
        pass
    BoundHandler.engine = engine
    return BoundHandler
