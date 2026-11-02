# -*- coding: utf-8 -*-
"""验收自检：在**数据库副本**上起一个临时实例，把关键链路逐项跑一遍，输出通过/失败清单。

用法：
    python3 验收自检.py                       # 复制当前库到临时目录，跑全部检查
    python3 验收自检.py --db /path/to.sqlite3 # 指定要检验的库（仍会复制一份，不改原库）
    python3 验收自检.py --concurrency 5       # 并发用户数（默认 5）
    python3 验收自检.py --keep                # 保留临时目录（排错用）
    python3 验收自检.py --report 清单.md      # 指定清单输出路径

设计要点：
1. **不碰交付目录**：库文件复制到临时目录，上传原件也写到临时目录（POLICY_DOCS_DIR），
   检查结束后整个临时目录删除；项目目录里不会出现 documents/、也不会多出上传文件。
2. **只清理本次产生的记录**：上传返回的 planid 逐个登记，清理时按 planid 精确删除
   documents / chunks / chunks_fts 三类记录（fts 按 rowid 独立存，必须一起删）。
3. 全程留痕：通过/失败逐项写入 Markdown 清单，失败时附服务端日志尾部。
全部通过退出码 0，否则 1，可直接接入 CI。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / '应用源码_示例知识库版.py'
BUILDER = HERE / '构建示例知识库.py'
DEFAULT_DB = HERE / 'knowledge.sqlite3'
RESULTS = []
LOG_FILE = None
WORK = None  # 临时工作目录


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def call(base, path, payload=None, timeout=180, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(base + path, data=data,
                                 headers=headers or ({'Content-Type': 'application/json'} if data else {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print(('  [通过] ' if ok else '  [失败] ') + name + (f'　{detail}' if detail else ''))
    return ok


def prepare_workspace(source_db):
    """把库复制到临时目录，并给上传原件准备独立目录。"""
    global WORK, LOG_FILE
    WORK = Path(tempfile.mkdtemp(prefix='kgsz-selfcheck-'))
    target = WORK / 'knowledge.sqlite3'
    src = sqlite3.connect(f'file:{source_db}?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    (WORK / 'documents').mkdir(parents=True, exist_ok=True)
    LOG_FILE = WORK / 'server.log'
    # 只读打开（不写库），确认副本可用于服务
    con = sqlite3.connect(f'file:{target}?mode=ro', uri=True)
    counts = {t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
              for t in ('documents', 'chunks', 'chunks_fts')}
    # 副本里原本就有的上传记录（若有）绝不能被当成「本次产生」删掉。
    pre_uploads = {r[0] for r in con.execute("SELECT planid FROM documents WHERE split='uploads'")}
    con.close()
    return target, counts, pre_uploads


def start_server(port):
    env = dict(os.environ)
    env.update({
        'POLICY_PORT': str(port),
        'POLICY_HOST': '127.0.0.1',
        'POLICY_KB_DIR': str(WORK),
        'POLICY_DOCS_DIR': str(WORK / 'documents'),
        'RATE_LIMIT_PER_MIN': '0',
        'LOG_LEVEL': 'WARNING',
        # 审计日志也写进临时目录，否则会在交付目录里留下 audit.jsonl
        'AUDIT_LOG': str(WORK / 'audit.jsonl'),
    })
    env.pop('ACCESS_TOKEN', None)
    env['PYTHONPATH'] = ''
    env['PYTHONNOUSERSITE'] = '1'
    # 用文件收日志而不是 PIPE：PIPE 没人读，缓冲区写满会把服务端阻塞住，
    # 表现为「连接被无故关闭」；顺带在失败时能把服务端日志打出来。
    handle = open(LOG_FILE, 'w', encoding='utf-8')
    proc = subprocess.Popen([sys.executable, '-S', str(APP)], cwd=str(HERE), env=env,
                            stdout=handle, stderr=subprocess.STDOUT)
    base = f'http://127.0.0.1:{port}'
    for _ in range(80):
        time.sleep(0.35)
        try:
            code, _, _ = call(base, '/healthz', timeout=3)
            if code in (200, 503):
                return proc, base
        except (urllib.error.URLError, ConnectionError, OSError):
            continue
    proc.terminate()
    raise SystemExit('实例启动超时；请先运行 构建示例知识库.py 生成 knowledge.sqlite3。')


def server_log_tail(n=25):
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as handle:
            return ''.join(handle.readlines()[-n:])
    except Exception:
        return ''


def run_builder(extra):
    """在副本上跑构建脚本的子命令，并把嵌入相关环境变量清空以固定判据。"""
    env = dict(os.environ)
    env.update({'POLICY_KB_DIR': str(WORK), 'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'})
    for key in ('EMBEDDING_API_KEY', 'LLM_API_KEY', 'EMBEDDING_BASE_URL', 'LLM_BASE_URL'):
        env.pop(key, None)
    return subprocess.run([sys.executable, '-S', str(BUILDER)] + list(extra), cwd=str(HERE), env=env,
                          capture_output=True, text=True, timeout=180)


def purge_upload(planids):
    """按 planid 精确删除本次上传产生的三类记录，并返回残留统计。"""
    if not planids:
        return 0, 0
    con = sqlite3.connect(WORK / 'knowledge.sqlite3')
    removed = 0
    for planid in planids:
        ids = [r[0] for r in con.execute('SELECT id FROM chunks WHERE planid=?', (planid,))]
        if ids:
            con.executemany('DELETE FROM chunks_fts WHERE rowid=?', [(i,) for i in ids])
        cur = con.execute('DELETE FROM chunks WHERE planid=?', (planid,))
        removed += cur.rowcount
        con.execute('DELETE FROM documents WHERE planid=?', (planid,))
    con.commit()
    orphan = con.execute('SELECT count(*) FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)'
                         ).fetchone()[0]
    marks = ','.join('?' * len(planids))
    leftover = con.execute(f'SELECT count(*) FROM documents WHERE planid IN ({marks})', list(planids)).fetchone()[0]
    stray_chunks = con.execute(f'SELECT count(*) FROM chunks WHERE planid IN ({marks})', list(planids)).fetchone()[0]
    con.close()
    return orphan, leftover + stray_chunks


def write_report(path, args, source_db, copy_counts, elapsed, server_log=''):
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [
        '# 验收自检清单',
        '',
        f'- 运行时间：{time.strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 运行环境：{platform.platform()}｜Python {platform.python_version()}',
        f'- 数据库副本：`{WORK / "knowledge.sqlite3"}`（源：`{source_db}`）',
        '- 源库是否被动过：由「自检未改动交付目录」一项的前后快照（大小 + 修改时间）覆盖',
        f'- 副本基线：{copy_counts["documents"]} 篇 / {copy_counts["chunks"]} 切片 / {copy_counts["chunks_fts"]} 索引',
        f'- 并发用户数：{args.concurrency}',
        f'- 总耗时：{elapsed:.1f} 秒',
        '',
        '## 逐项结果',
        '',
        '| # | 结果 | 检查项 | 详情 |',
        '|---|---|---|---|',
    ]
    for i, (name, ok, detail) in enumerate(RESULTS, 1):
        lines.append(f'| {i} | {"通过" if ok else "**失败**"} | {name} | {detail.replace("|", "/")} |')
    lines += ['', f'## 汇总：**{passed}/{total} 通过**' + ('，全部通过。' if passed == total else '，存在失败项。'), '']
    if server_log:
        lines += ['## 服务端日志（末尾）', '', '```', server_log.rstrip(), '```', '']
    Path(path).write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--db', default=str(DEFAULT_DB), help='要检验的知识库（会复制副本，不改原库）')
    parser.add_argument('--concurrency', type=int, default=5, help='并发用户数，默认 5')
    parser.add_argument('--keep', action='store_true', help='保留临时目录便于排错')
    parser.add_argument('--report', default='', help='清单输出路径（默认 验收清单_时间戳.md）')
    args = parser.parse_args()
    source_db = Path(args.db).expanduser().resolve()
    if not source_db.is_file():
        raise SystemExit(f'找不到知识库：{source_db}')

    before = {p.name: (p.stat().st_size, int(p.stat().st_mtime)) for p in HERE.iterdir()}
    copy_db, copy_counts, pre_uploads = prepare_workspace(source_db)
    started = time.time()
    port = args.port or free_port()
    proc, base = start_server(port)
    print(f'\n验收自检（实例 {base}）')
    print(f'  数据库副本：{copy_db}')
    print('=' * 62)
    upload_ids, stray_files = [], []
    try:
        code, body, _ = call(base, '/healthz')
        health = json.loads(body)
        check('服务健康检查', code == 200 and health.get('fts_indexed') == health.get('chunks'),
              f"{health.get('documents')} 篇 / {health.get('chunks')} 切片 / 索引 {health.get('fts_indexed')}")
        check('健康检查报告语义检索状态', 'semantic_active' in health and 'embedding_model' in health,
              f"模型 {health.get('embedding_model')}，已覆盖 {health.get('embedded')}/{health.get('ready_chunks')}，"
              f"语义检索{'启用' if health.get('semantic_active') else '未启用'}")
        # 覆盖不全时绝不能声称已启用：部分向量会把排序偏向前几千条切片。
        covered = health.get('embedded') or 0
        ready_chunks = health.get('ready_chunks') or 0
        check('语义开关与向量覆盖度自洽',
              bool(health.get('semantic_active')) == bool(covered >= ready_chunks and covered > 0),
              f"覆盖 {covered}/{ready_chunks}，维度 {health.get('embedding_dim')}，"
              f"开关 {health.get('semantic_active')}")
        verify = run_builder(['--verify-embeddings'])
        verify_out = (verify.stdout or '') + (verify.stderr or '')
        ready_hit = re.search(r'参与检索\(ready\)\s*(\d+)', verify_out)
        check('向量覆盖核对命令可用且与 healthz 一致',
              verify.returncode == 0 and ready_hit is not None
              and int(ready_hit.group(1)) == ready_chunks,
              f"退出码 {verify.returncode}，ready {ready_hit.group(1) if ready_hit else '?'} vs healthz {ready_chunks}")

        code, body, _ = call(base, '/')
        page = body.decode('utf-8', 'replace')
        check('首页可访问', code == 200 and '管策智研' in page, f'{len(body)} 字节')

        q = '沧州市十五规划产业结构调整的主要方向是什么'
        code, body, _ = call(base, '/api/ask', {'question': q, 'session_id': 'selfcheck'})
        ans = json.loads(body) if code == 200 else {}
        srcs = ans.get('sources') or []
        triple = srcs and all(k in srcs[0] for k in ('filename', 'heading', 'ordinal', 'fragment', 'planid'))
        check('问答接口返回证据', code == 200 and bool(srcs), f'{len(srcs)} 条证据，状态 {ans.get("status")}')
        check('片段级溯源字段完整', triple, '文件名/章节/段落/片段/planid')

        if srcs:
            s = srcs[0]
            code, body, _ = call(base, f"/source/{urllib.parse.quote(s['planid'])}?frag={s['fragment']}")
            html = body.decode('utf-8', 'replace')
            check('原文回看可定位到片段', code == 200 and 'kgsz-bar' in html and '__KGSZ_NEEDLE__' in html,
                  f'片段 {s["fragment"]}，{len(body)} 字节')

        code, body, _ = call(base, '/api/extract', {'question': '沧州市产业结构调整的量化目标、重点产业、政策工具和责任主体'})
        ex = json.loads(body) if code == 200 else {}
        table = ex.get('table') or []
        dims = {r['dim'] for r in table}
        check('四要素结构化抽取', code == 200 and len(table) >= 4 and len(dims) >= 3,
              f'{len(table)} 行，覆盖 {len(dims)} 个维度，状态 {ex.get("status")}')
        check('四要素均带原文引文', bool(table) and all(r.get('quote') for r in table))

        code, body, _ = call(base, '/api/evolution', {'question': '沧州市产业结构调整的重点任务'})
        ev = json.loads(body) if code == 200 else {}
        periods = ev.get('periods') or []
        check('跨期演变可按期对照', code == 200 and len(periods) >= 2,
              '、'.join(f'{p["label"]}({p["start_year"]})' for p in periods[:4]))

        code, body, _ = call(base, '/api/compare', {'question': '比较沧州市和衡水市在产业结构调整上的部署'})
        cmp_ = json.loads(body) if code == 200 else {}
        docs = {s['planid'] for s in (cmp_.get('sources') or [])}
        check('多文件比较覆盖多个文件', code == 200 and len(docs) >= 2, f'{len(docs)} 份文件')

        code, body, _ = call(base, '/api/ask', {'question': '沧州市产业结构调整的量化目标是什么', 'session_id': 'selfcheck2'})
        code2, body2, _ = call(base, '/api/ask', {'question': '那重点产业呢', 'session_id': 'selfcheck2'})
        follow = json.loads(body2) if code2 == 200 else {}
        check('多轮追问继承上下文', code2 == 200 and bool(follow.get('rewritten_query')),
              (follow.get('rewritten_query') or '')[:48])

        export_id = (ex.get('export_id') or ans.get('export_id') or '')
        for fmt, must in (('docx', ['word/document.xml', 'word/styles.xml', 'word/footer1.xml']),
                          ('xlsx', ['xl/workbook.xml', 'xl/worksheets/sheet1.xml', 'xl/styles.xml']),
                          ('pptx', ['ppt/presentation.xml', 'ppt/slides/slide1.xml'])):
            code, body, headers = call(base, f'/api/export/{fmt}', {'export_id': export_id})
            ok = code == 200 and body[:2] == b'PK'
            parts = []
            if ok:
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    parts = z.namelist()
                    ok = all(m in parts for m in must) and z.testzip() is None
            check(f'{fmt.upper()} 导出可打开', ok, f'{len(body)/1024:.0f} KB，{len(parts)} 个部件')

        code, body, _ = call(base, '/api/export/csv', {'export_id': export_id})
        rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig')))) if code == 200 else []
        check('CSV 导出含四要素表', code == 200 and any('量化目标' in (c or '') for r in rows for c in r),
              f'{len(rows)} 行')

        boundary = '----selfcheck' + uuid.uuid4().hex
        content = '沧州市自检样例文档\n\n一、产业方向\n重点培育绿色化工与高端装备制造产业集群，到2025年产值达到800亿元。\n'.encode()
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="自检样例.txt"\r\n'
                   'Content-Type: text/plain\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        code, body, _ = call(base, '/api/upload', raw=payload,
                             headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        up = json.loads(body) if code == 200 else {}
        check('上传后可立即检索', code == 200 and '已建索引' in (up.get('message') or ''), up.get('message', '')[:40])

        # 登记本次上传产生的记录与原文件，供精确清理（不按大小/时间猜别人的文件）
        for item in (up.get('documents') or []):
            if isinstance(item, dict) and item.get('planid'):
                upload_ids.append(item['planid'])
                if item.get('source_path'):
                    stray_files.append(str((WORK / 'documents' / item['source_path']).resolve()))
        if not upload_ids:
            # 接口未回传标识时，用「副本里新增的上传记录」兜底，绝不碰副本原有的上传。
            con = sqlite3.connect(WORK / 'knowledge.sqlite3')
            current = {r[0] for r in con.execute("SELECT planid FROM documents WHERE split='uploads'")}
            con.close()
            upload_ids = sorted(current - pre_uploads)

        code, body, _ = call(base, '/api/ask', {'question': '自检样例文档里重点培育什么产业集群'})
        found = json.loads(body) if code == 200 else {}
        names = [s.get('filename', '') for s in (found.get('sources') or [])]
        check('新上传内容进入检索结果', code == 200 and any('自检样例' in n for n in names),
              f'{len(names)} 条证据')

        # ---- 5 用户并发（同一实例，不同问题） ----
        questions = ['沧州市十五规划产业结构调整的主要方向是什么',
                     '衡水市十三五规划的重点任务有哪些',
                     '河北省十四五规划中关于生态环境的部署',
                     '第十四个五年规划里民生保障的目标',
                     '雄安新区规划中基础设施建设的要求']
        users = max(2, args.concurrency)

        def one(item):
            i, question = item
            t0 = time.perf_counter()
            code, body, _ = call(base, '/api/ask', {'question': question, 'session_id': f'conc{i}'}, timeout=180)
            dt = time.perf_counter() - t0
            got = len((json.loads(body).get('sources') or [])) if code == 200 else 0
            return code, dt, got

        t_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=users) as pool:
            outcomes = list(pool.map(one, list(enumerate(questions * (users // len(questions) + 1)))[:users]))
        wall = time.perf_counter() - t_start
        slowest = max(dt for _, dt, _ in outcomes)
        ok_all = all(c == 200 and n > 0 for c, _, n in outcomes)
        check(f'{users} 用户并发问答', ok_all,
              f'全部 {len(outcomes)} 请求成功，总墙钟 {wall:.2f}s，单请求最慢 {slowest*1000:.0f}ms')
        check('并发下满足端到端时限（≤180s）', wall < 180, f'{wall:.2f}s')

        # ---- 边界与安全 ----
        code, body, _ = call(base, '/api/ask', {'question': ''})
        check('空问题被拒', code in (400, 422), f'HTTP {code}')
        code, body, _ = call(base, '/api/ask', raw=b'{bad json',
                             headers={'Content-Type': 'application/json'})
        check('非法 JSON 被拒且不泄漏内部信息', code in (400, 500) and b'Traceback' not in body, f'HTTP {code}')
        code, body, _ = call(base, '/api/ask', {'question': '《不存在的政策文件》是否有关于产业补贴的规定'})
        nores = json.loads(body) if code == 200 else {}
        check('未收录文件的问题明确拒答', code == 200 and nores.get('status') in
              ('inconclusive', 'no_evidence', 'insufficient', 'not_found_in_complete_scope'),
              f'状态 {nores.get("status")}')
        code, body, _ = call(base, '/api/ask', {'question': '火星殖民计划第三期的预算安排'})
        off = json.loads(body) if code == 200 else {}
        check('无关主题不输出结论', code == 200 and off.get('status') in
              ('no_evidence', 'evidence_only', 'insufficient', 'inconclusive'),
              f'状态 {off.get("status")}')
        code, body, _ = call(base, '/source/../../etc/passwd')
        check('原文路径穿越被拦截', code in (400, 404, 409), f'HTTP {code}')

        # ---- 对外监听必须带访问控制 ----
        guard_port = free_port()
        env = dict(os.environ)
        env.update({'POLICY_PORT': str(guard_port), 'POLICY_HOST': '0.0.0.0',
                    'POLICY_KB_DIR': str(WORK), 'POLICY_DOCS_DIR': str(WORK / 'documents'),
                    'LOG_LEVEL': 'ERROR'})
        env.pop('ACCESS_TOKEN', None)
        guard_log = WORK / 'guard.log'
        with open(guard_log, 'w', encoding='utf-8') as gh:
            gp = subprocess.run([sys.executable, '-S', str(APP)], cwd=str(HERE), env=env,
                                stdout=gh, stderr=subprocess.STDOUT, timeout=60)
        text = guard_log.read_text(encoding='utf-8', errors='replace')
        listening = False
        try:
            with socket.create_connection(('127.0.0.1', guard_port), timeout=1):
                listening = True
        except OSError:
            pass
        check('非回环监听缺访问控制时拒绝启动',
              gp.returncode != 0 and not listening and '拒绝启动' in text,
              f'退出码 {gp.returncode}，端口{"仍可连接" if listening else "未监听"}')

    except Exception as exc:  # 任何未预期异常都要落成一条失败项，并带出服务端日志
        check('检查流程未中断', False, f'{type(exc).__name__}: {exc}')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

        # ---- 清理：只清本次上传的记录，且只在副本上操作 ----
        try:
            orphan, leftover = purge_upload(upload_ids)
            con = sqlite3.connect(f'file:{WORK / "knowledge.sqlite3"}?mode=ro', uri=True)
            after = {t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                     for t in ('documents', 'chunks', 'chunks_fts')}
            con.close()
            restored = after == copy_counts
            check('清理后副本记录回到基线', restored and orphan == 0 and leftover == 0,
                  f'文档 {after["documents"]}/{copy_counts["documents"]}，切片 {after["chunks"]}/{copy_counts["chunks"]}，'
                  f'索引 {after["chunks_fts"]}/{copy_counts["chunks_fts"]}，孤立索引 {orphan}')
            if stray_files:
                for path in stray_files:
                    try:
                        Path(path).unlink()
                    except OSError:
                        pass
            remaining = [p for p in (WORK / 'documents').rglob('*') if p.is_file()]
            check('只清理本次生成的文件', all(not Path(p).exists() for p in stray_files) and not remaining,
                  f'本次上传原件 {len(stray_files)} 个已删除，目录残留 {len(remaining)} 个')
        except Exception as exc:
            check('清理流程未中断', False, f'{type(exc).__name__}: {exc}')

        # ---- 隔离性：调用方目录不应被写入 ----
        after_here = {p.name: (p.stat().st_size, int(p.stat().st_mtime)) for p in HERE.iterdir()}
        changed = [k for k in before if k in after_here and after_here[k] != before[k]]
        added = [k for k in after_here if k not in before and not k.startswith('验收清单')]
        check('自检未改动交付目录', not changed and not added,
              f'新增 {added or "无"}，改写 {changed or "无"}')

        if not args.keep and WORK.exists():
            shutil.rmtree(WORK, ignore_errors=True)
        elif args.keep:
            print(f'  [保留] 临时目录：{WORK}')

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print('=' * 62)
    print(f'结果：{passed}/{total} 项通过' + ('　全部通过' if passed == total else '　存在失败项'))
    report = Path(args.report) if args.report else HERE / f'验收清单_{time.strftime("%Y%m%d-%H%M%S")}.md'
    write_report(report, args, source_db, copy_counts, time.time() - started,
                 server_log_tail() if passed != total else '')
    print(f'清单已保存：{report}')
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
