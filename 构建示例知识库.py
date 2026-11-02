# -*- coding: utf-8 -*-
"""Build a searchable SQLite knowledge base from the PlanDB sample dataset."""
from pathlib import Path
import csv
import os
import re
import shutil
import sqlite3
import json
import struct
import sys
import tempfile
import time
import http.client
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from urllib.error import HTTPError, URLError

PROJECT = Path(__file__).resolve().parent
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


def open_api(req, timeout=90):
    """本机回环地址的嵌入服务要绕过系统代理，否则请求会发给 HTTP_PROXY 而不是本地端口。"""
    host = (urlparse(req.full_url).hostname or '').strip().lower()
    if host in ('127.0.0.1', 'localhost', '::1') or host.startswith('127.'):
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urlopen(req, timeout=timeout)


def load_env_file(path):
    """Load KEY=VALUE pairs from an optional .env file.

    已存在的环境变量优先，但**空值不算已设置**：shell 模板或 CI 常先
    `export LLM_API_KEY=`（空串），按「key in os.environ」判断会把 .env 里的
    真实密钥挡掉，首次配置被误判成「未配置」。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if (os.environ.get(key) or '').strip():
            continue
        os.environ[key] = value


load_env_file(PROJECT / '.env')
load_env_file(PROJECT / 'config.env')


def first_dir(*candidates):
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path.resolve()
    return None


# Dataset and output locations are auto-detected so the package is portable.
DATASET = first_dir(
    os.getenv('POLICY_DATASET_DIR'),
    PROJECT / '管策智研_示例数据集',
    PROJECT.parent / '管策智研_示例数据集',
    PROJECT.parent / '管策智研检验所用数据' / '管策智研_示例数据集',
    Path.home() / 'Desktop' / '管策智研检验所用数据' / '管策智研_示例数据集',
    Path.home() / 'Desktop' / '管策智研_示例数据集',
) or (PROJECT / '管策智研_示例数据集')
_nested_kb = PROJECT / '知识库_示例数据集_v1'
OUTPUT = Path(os.getenv('POLICY_KB_DIR') or (
    PROJECT if (PROJECT / 'knowledge.sqlite3').is_file()
    else _nested_kb if (_nested_kb / 'knowledge.sqlite3').is_file()
    else PROJECT)).expanduser().resolve()
DB = OUTPUT / 'knowledge.sqlite3'
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
# 可选：显式声明期望维度。设了就会校验库内向量维度，防止换模型后新旧向量混用。
EMBEDDING_DIM = int(os.getenv('EMBEDDING_DIM', '0') or 0)
EMBEDDING_BATCH_SIZE = max(1, min(128, int(os.getenv('EMBEDDING_BATCH_SIZE', '64'))))
EMBEDDING_CONCURRENCY = max(1, min(8, int(os.getenv('EMBEDDING_CONCURRENCY', '4'))))
# 切块窗口：过短则证据缺上下文，过长则单块主题发散。450–800 字是中文政策文本的
# 折中：一段完整条款通常在此区间，检索命中后可直接作为引用原文。
CHUNK_MIN = max(200, int(os.getenv('CHUNK_MIN_CHARS', '450')))
CHUNK_MAX = max(CHUNK_MIN + 100, int(os.getenv('CHUNK_MAX_CHARS', '800')))
CHUNK_OVERLAP = max(0, min(200, int(os.getenv('CHUNK_OVERLAP_CHARS', '60'))))


def embedding_key():
    return (os.getenv('EMBEDDING_API_KEY') or os.getenv('LLM_API_KEY', '')).strip()


def embedding_base():
    return (os.getenv('EMBEDDING_BASE_URL') or os.getenv('LLM_BASE_URL',
                                                         'https://api.openai.com/v1')).rstrip('/')


def is_loopback_host(host):
    host = (host or '').strip().strip('[]').lower()
    return host in ('127.0.0.1', 'localhost', '::1') or host.startswith('127.')


def embedding_enabled():
    """有密钥就启用；**回环地址的自建服务（Ollama / LM Studio / vLLM / TEI）不需要密钥**。

    原来只认密钥，导致唯一能离线完成向量回填的路径被堵死：本机服务不校验
    Authorization，用户会以为「没密钥就不能做语义检索」。
    """
    if embedding_key():
        return True
    return is_loopback_host(urlparse(embedding_base()).hostname)


def read_front_matter(text):
    if not text.startswith('---'):
        return {}
    end = text.find('\n---', 3)
    if end < 0:
        return {}
    values = {}
    for line in text[4:end].splitlines():
        match = re.match(r'([^:]+):\s*(.*)$', line)
        if match:
            values[match.group(1).strip()] = match.group(2).strip().strip('"')
    return values


def read_manifest():
    path = DATASET / 'metadata' / 'sample_plans.csv'
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return {row['planid']: row for row in csv.DictReader(handle)}


STOP_TOKENS = frozenset('''
的 是 和 与 及 在 有 对 为 中 了 这 那 个 我们 可以 需要 请问 一下 如何 怎么 什么 哪些 主要
以及 或者 但是 因为 所以 如果 通过 进行 关于 情况 是否 包括 包含 具有 方面 相关 有关 问题
内容 介绍 说明 简述 概述 谈谈 列出 给出 指出 分析 哪些 怎么样 有没有 请 简要 具体 分别 根据
'''.split())


def tokenize(text):
    """必须与应用源码中的 tokens() 保持完全一致，否则索引与查询词不匹配。"""
    text = text.lower()
    out = set(re.findall(r'[a-z0-9][a-z0-9._-]*', text))
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return {t for t in out if t not in STOP_TOKENS}


def split_markdown(planid, text):
    """按 Markdown 标题与段落切块；逻辑必须与应用源码 split_chunks 完全一致。"""
    body = text
    if body.startswith('---'):
        end = body.find('\n---', 3)
        body = body[end + 4:] if end >= 0 else body
    headings, paragraphs, buffer = [], [], []

    def flush():
        if buffer:
            value = re.sub(r'\s+', ' ', ' '.join(buffer)).strip()
            if value:
                paragraphs.append((' / '.join(headings), value))
            buffer.clear()

    for raw in body.replace('\r', '').splitlines():
        line = raw.strip()
        match = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        numbered = re.match(r'^(第[一二三四五六七八九十百0-9]+[章节篇部分]|[一二三四五六七八九十]+、|\d+(?:\.\d+)*\s+)(.+)$', line)
        if match or (numbered and len(line) < 100):
            flush()
            level, title = (len(match.group(1)), match.group(2).strip()) if match else (1, numbered.group(2).strip())
            headings[:] = headings[:level - 1]
            headings.append(title)
        elif raw.strip():
            buffer.append(raw.strip())
        else:
            flush()
    flush()

    blocks, index = [], 0
    # 同一标题下的相邻段落先合并到目标长度，超长段落再按句号切分并保留重叠。
    # 逐段切块会让 65% 的切片不足 200 字，检索与生成两边都吃亏。
    while index < len(paragraphs):
        heading, block = paragraphs[index]
        ordinal = index + 1
        nxt = index + 1
        while nxt < len(paragraphs) and paragraphs[nxt][0] == heading and len(block) < CHUNK_MIN:
            merged = block + '\n' + paragraphs[nxt][1]
            if len(merged) > CHUNK_MAX:
                break
            block = merged
            nxt += 1
        if len(block) <= CHUNK_MAX:
            blocks.append((heading or '正文', ordinal, block))
        else:
            start = 0
            while start < len(block):
                end = min(len(block), start + CHUNK_MAX)
                if end < len(block):
                    boundary = max(block.rfind(mark, start + CHUNK_MIN // 2, end) for mark in '。！？；\n')
                    if boundary > start:
                        end = boundary + 1
                piece = block[start:end].strip()
                if piece:
                    blocks.append((heading or '正文', ordinal, piece))
                if end >= len(block):
                    break
                start = max(end - CHUNK_OVERLAP, start + 1)
        index = nxt
    # 章节末尾常剩一段几十字的收尾语，单独成块几乎没有检索价值，回并到上一块。
    merged_blocks = []
    for heading, ordinal, piece in blocks:
        if (merged_blocks and len(piece) < CHUNK_MIN
                and merged_blocks[-1][0] == heading
                and len(merged_blocks[-1][2]) + len(piece) + 1 <= CHUNK_MAX):
            ph, po, pt = merged_blocks[-1]
            merged_blocks[-1] = (ph, po, pt + '\n' + piece)
        else:
            merged_blocks.append((heading, ordinal, piece))
    return [(planid, h, o, i + 1, t) for i, (h, o, t) in enumerate(merged_blocks)]


TOPIC_RULES = (
    ('政府综合', ('政府工作报告', '国民经济和社会发展', '民生实事', '政府自身建设')),
    ('农业农村', ('农业', '农村', '乡村', '粮食', '农民', '扶贫', '脱贫', '水利', '畜牧', '渔业', '林业')),
    ('生态环境', ('生态', '环境', '污染', '节能', '减排', '碳', '绿化', '环保', '资源节约', '循环经济')),
    ('科教文卫', ('教育', '科技', '文化', '体育', '人才', '卫生', '医疗', '健康', '计划生育', '广播电视')),
    ('民生保障', ('民政', '养老', '社会保障', '就业', '住房', '人口', '残疾', '退役军人', '妇女儿童', '收入分配')),
    ('基础设施', ('交通', '公路', '铁路', '机场', '港口', '城市', '城镇', '能源', '电力', '供水', '管网', '国土', '防灾')),
    ('经济产业', ('产业', '工业', '经济', '制造', '开发区', '园区', '招商', '商贸', '物流', '金融', '旅游', '数字经济', '国有企业')),
)


def topic_of(title, fyp_name, sample):
    """按标题与正文开头的关键词判定主题领域。

    数据源（corpus/edge_cases）已单独存在 split 列里，不必再占用大类字段；
    大类应该回答"这篇讲什么领域"，才能支撑按专题检索与跨专题对比。
    """
    title_text = title or ''
    body_text = (sample or '')[:1500]
    best, best_score = '其他', 0
    for topic, keys in TOPIC_RULES:
        score = 0
        for key in keys:
            if key in title_text:
                score += 3
            score += body_text.count(key) / 8
        if score > best_score:
            best, best_score = topic, score
    if best_score < 1 and fyp_name and '政府工作报告' in fyp_name:
        return '政府综合'
    return best


def _check_vectors(result, batch):
    """接口偶尔会返回全零、NaN 或数量不符的向量；这些写进库会让余弦相似度整列失效。"""
    if len(result) != len(batch):
        raise RuntimeError(f'接口返回 {len(result)} 个向量，预期 {len(batch)} 个')
    dims = {len(v) for v in result}
    if len(dims) != 1:
        raise RuntimeError(f'同一批返回的向量维度不一致：{sorted(dims)}')
    dim = dims.pop()
    if dim <= 0:
        raise RuntimeError('接口返回了空向量')
    if EMBEDDING_DIM and dim != EMBEDDING_DIM:
        raise RuntimeError(f'接口返回 {dim} 维向量，与 EMBEDDING_DIM={EMBEDDING_DIM} 不符')
    for vec in result:
        norm = 0.0
        for x in vec:
            if not isinstance(x, (int, float)) or x != x or x in (float('inf'), float('-inf')):
                raise RuntimeError('接口返回的向量含非数值或 NaN/Inf')
            norm += x * x
        if norm <= 0:
            raise RuntimeError('接口返回了全零向量')
    return dim


def embed_texts(texts):
    key = embedding_key()
    if not texts or not embedding_enabled():
        return []
    base = embedding_base()
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    vectors = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start:start + EMBEDDING_BATCH_SIZE]
        payload = json.dumps({'model': EMBEDDING_MODEL, 'input': batch}).encode()
        req = Request(base + '/embeddings', data=payload, headers=headers)
        for attempt in range(5):
            try:
                with open_api(req, timeout=90) as response:
                    data = json.loads(response.read())
                result = [item['embedding'] for item in sorted(data['data'], key=lambda x: x['index'])]
                _check_vectors(result, batch)
                vectors.extend(result)
                break
            except RuntimeError:
                raise
            except HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                    detail = exc.read().decode('utf-8', errors='replace')[:1200]
                    raise RuntimeError(f'嵌入接口调用失败（{EMBEDDING_MODEL}）：HTTP {exc.code} {detail}')
                time.sleep(min(2 ** attempt, 30))
            except (URLError, TimeoutError, http.client.HTTPException, ConnectionError, OSError,
                    KeyError, IndexError, TypeError, ValueError) as exc:
                if attempt == 4:
                    raise RuntimeError(f'嵌入接口调用失败（{EMBEDDING_MODEL}）：{exc}')
                time.sleep(min(2 ** attempt, 30))
    return vectors


def require_embedding_config():
    """回填前置检查：放在备份之前，别让一次误调用先落个 163 MB 的空备份。"""
    if embedding_enabled():
        return
    raise SystemExit(
        '没有可用的嵌入接口，二选一：\n'
        '  · 云端：设置 EMBEDDING_API_KEY（或复用 LLM_API_KEY），必要时配 EMBEDDING_BASE_URL / EMBEDDING_MODEL；\n'
        '  · 本机：把 EMBEDDING_BASE_URL 指到回环地址（如 http://127.0.0.1:11434/v1）可免密钥。')


def backfill_embeddings():
    if not DB.exists():
        raise SystemExit(f'找不到知识库：{DB}')
    require_embedding_config()
    con = sqlite3.connect(DB)
    con.execute('ALTER TABLE chunks ADD COLUMN embedding BLOB') if 'embedding' not in {r[1] for r in con.execute('PRAGMA table_info(chunks)')} else None
    con.execute('ALTER TABLE chunks ADD COLUMN embedding_model TEXT') if 'embedding_model' not in {r[1] for r in con.execute('PRAGMA table_info(chunks)')} else None
    existing = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND embedding_model=?', (EMBEDDING_MODEL,)).fetchone()[0]
    total = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NULL OR embedding_model<>?', (EMBEDDING_MODEL,)).fetchone()[0]
    target_total = existing + total
    if not total:
        print(f'全部 {target_total} 条切片都已有 {EMBEDDING_MODEL} 的向量，无需回填。')
        con.close()
        return
    print(f'嵌入接口：{embedding_base()}  模型：{EMBEDDING_MODEL}  批 {EMBEDDING_BATCH_SIZE} × 并发 {EMBEDDING_CONCURRENCY}')
    print(f'待回填 {total} 条，已有 {existing} 条，合计 {target_total} 条')
    started = time.time()
    first_pass = existing
    last_print = 0.0
    dim_seen = None
    while True:
        rows = con.execute('SELECT id,heading,text FROM chunks WHERE embedding IS NULL OR embedding_model<>? ORDER BY id LIMIT ?',
                           (EMBEDDING_MODEL, EMBEDDING_BATCH_SIZE * EMBEDDING_CONCURRENCY)).fetchall()
        if not rows:
            break
        batches = [rows[i:i + EMBEDDING_BATCH_SIZE] for i in range(0, len(rows), EMBEDDING_BATCH_SIZE)]
        failure = None
        with ThreadPoolExecutor(max_workers=min(EMBEDDING_CONCURRENCY, len(batches))) as pool:
            pending = {pool.submit(embed_texts, [heading + '\n' + text for _, heading, text in batch]): batch
                       for batch in batches}
            for future in as_completed(pending):
                batch = pending[future]
                try:
                    vectors = future.result()
                    # 同一模型名下混入不同维度，余弦相似度会整列错位；宁可中断也不写脏数据。
                    if dim_seen is None and vectors:
                        dim_seen = len(vectors[0])
                    elif vectors and dim_seen and len(vectors[0]) != dim_seen:
                        raise RuntimeError(f'同一模型中混入不同维度（{dim_seen} vs {len(vectors[0])}），'
                                           f'请确认 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 指向同一服务')
                    con.executemany('UPDATE chunks SET embedding=?,embedding_model=? WHERE id=?',
                                    [(struct.pack('<%sf' % len(v), *v), EMBEDDING_MODEL, row[0])
                                     for row, v in zip(batch, vectors)])
                    con.commit()
                    existing += len(batch)
                except Exception as exc:
                    failure = exc
        if failure:
            done = existing
            con.close()
            sys.stdout.flush()
            print(f'\n回填中断：已完成 {done}/{target_total}（{done / target_total:.1%}）。'
                  f'已写入的向量不会丢，修好接口后重跑同一条命令即从未完成的切片续跑。\n原因：{failure}',
                  file=sys.stderr)
            raise SystemExit(1)
        elapsed = max(time.time() - started, 1e-6)
        rate = (existing - first_pass) / elapsed
        remain = target_total - existing
        eta = f'{remain / rate / 60:.1f} 分钟' if rate > 0 else '未知'
        # 每 2 秒刷一次：重定向到日志时不会刷几万行，交互运行时仍有实时进度。
        if time.time() - last_print >= 2 or not remain:
            last_print = time.time()
            print(f'已生成 {existing}/{target_total}（{existing / target_total:.1%}），'
                  f'{rate:.0f} 条/秒，预计剩余 {eta}', end='\r', flush=True)
    con.close()
    elapsed = max(time.time() - started, 1e-6)
    print(f'\n完成：{existing}/{target_total} 条，维度 {dim_seen or EMBEDDING_DIM}，'
          f'耗时 {elapsed / 60:.1f} 分钟（{(existing - first_pass) / elapsed:.0f} 条/秒）')


def build():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # 原地重建：删除数据库文件在多数环境里会被权限/同步盘拦住，而且会留下
    # -wal/-shm 孤儿文件。直接 DROP 三张表再建，效果等价且可反复执行。
    if DB.exists():
        con = sqlite3.connect(DB)
        for table in ('chunks', 'chunks_fts', 'documents'):
            con.execute(f'DROP TABLE IF EXISTS {table}')
        con.commit()
        con.close()
    for suffix in ('-wal', '-shm'):
        stale = Path(str(DB) + suffix)
        if stale.exists():
            stale.unlink(missing_ok=True)
    metadata = read_manifest()
    con = sqlite3.connect(DB)
    con.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE documents (
        planid TEXT PRIMARY KEY, split TEXT NOT NULL, title TEXT NOT NULL,
        area_name TEXT, admin_level TEXT, parent_area_name TEXT, fyp_name TEXT,
        plantype_label TEXT, library_fileext TEXT, content_source TEXT,
        source_path TEXT NOT NULL, status TEXT NOT NULL,
        top_category TEXT NOT NULL, subcategory TEXT, subsubcategory TEXT,
        source_layer TEXT NOT NULL, text_len INTEGER NOT NULL
      );
      CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, planid TEXT NOT NULL REFERENCES documents(planid),
        filename TEXT NOT NULL, heading TEXT NOT NULL, ordinal INTEGER NOT NULL,
        fragment INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB, embedding_model TEXT,
        UNIQUE(planid, fragment)
      );
      CREATE INDEX idx_chunks_planid ON chunks(planid);
      CREATE INDEX idx_chunks_heading ON chunks(heading);
      CREATE INDEX idx_documents_category ON documents(top_category, subcategory, subsubcategory);
    ''')
    try:
        con.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(terms)")
    except sqlite3.OperationalError:
        con.execute('CREATE TABLE chunks_fts(rowid INTEGER PRIMARY KEY, terms TEXT)')
    counters = {'documents': 0, 'chunks': 0, 'empty': 0}
    for split in ('corpus', 'edge_cases'):
        for path in sorted((DATASET / split).glob('*/*.md')):
            planid = path.parent.name
            front = read_front_matter(path.read_text(encoding='utf-8', errors='replace'))
            meta = metadata.get(planid, {})
            text = path.read_text(encoding='utf-8', errors='replace')
            title = front.get('title') or meta.get('title') or planid
            area = front.get('area_name') or meta.get('area_name') or ''
            admin = front.get('admin_level') or meta.get('admin_level') or ''
            fyp = front.get('fyp_name') or meta.get('fyp_name') or ''
            plantype = front.get('plantype_label') or meta.get('plantype_label') or ''
            chunks = split_markdown(planid, text)
            status = 'ready' if chunks else 'empty'
            # 大类=主题领域，亚类=行政层级，三级=规划期；数据来源另存 split 列。
            topic = topic_of(title, fyp, text)
            values = (planid, split, title, area, admin,
                      front.get('parent_area_name') or meta.get('parent_area_name') or '',
                      fyp, plantype, front.get('library_fileext') or meta.get('library_fileext') or '',
                      front.get('content_source') or meta.get('content_source') or '',
                      str(path.relative_to(DATASET)), status, topic, admin or '未标注层级', fyp or '未标注规划期',
                      'L1正文Markdown', int(front.get('text_len') or meta.get('text_len') or 0))
            con.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
            counters['documents'] += 1
            counters['empty'] += status == 'empty'
            for (_, heading, ordinal, fragment, chunk_text) in chunks:
                cur = con.execute('INSERT INTO chunks(planid,filename,heading,ordinal,fragment,text) VALUES(?,?,?,?,?,?)',
                            (planid, path.name, heading, ordinal, fragment, chunk_text))
                terms = ' '.join(tokenize(heading + ' ' + title + ' ' + chunk_text))
                try:
                    con.execute('INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)', (cur.lastrowid, terms))
                except sqlite3.OperationalError:
                    pass
                counters['chunks'] += 1
    con.commit()
    con.close()
    # Vectorisation is a separate, resumable pass. Doing it inside the document
    # loop meant one API round-trip per file, which cannot scale to the full corpus.
    if embedding_enabled():
        print('开始生成切片向量（可中断，重跑自动续跑）…')
        try:
            backfill_embeddings()
            verify_embeddings(probe=True)
        except Exception as exc:
            # 向量失败不能连累建库：全文检索、溯源、导出都还能用，先让应用起得来。
            print(f'向量回填未完成：{exc}\n  全文索引已建好，可先启动应用；'
                  f'修好接口后运行 构建示例知识库.py --index-embeddings 续跑。', file=sys.stderr)
    else:
        print('未配置嵌入接口：本次只建立全文索引。'
              '配置 EMBEDDING_API_KEY（或把 EMBEDDING_BASE_URL 指到本机服务）后运行 '
              '构建示例知识库.py --index-embeddings 补向量。')
    (OUTPUT / '分类说明.md').write_text('''# 知识库分类说明

本库以每篇规划文本为一个 `documents` 记录，以 Markdown 正文切片为 `chunks` 记录。SQLite FTS5 提供 BM25 关键词召回；配置嵌入接口后，切片同时保存向量供应用执行混合检索。

## 一、文档级分类维度（四层）

| 字段 | 含义 | 取值来源 |
|---|---|---|
| `top_category` 大类 | **主题领域** | 标题与正文开头的领域关键词（经济产业 / 农业农村 / 生态环境 / 科教文卫 / 民生保障 / 基础设施 / 政府综合 / 其他） |
| `subcategory` 亚类 | **行政层级** | 元数据 `admin_level`：国家 / 省 / 市 / 区县 |
| `subsubcategory` 三级 | **规划期** | 元数据 `fyp_name`：十四五 / 十三五 / 十二五 / 政府工作报告等 |
| `plantype_label` 文种层 | 规划纲要 / 专项规划 / 其他 | 数据集元数据 |
| `source_layer` 来源层 | 统一 L1 正文 Markdown | 原始 Word/PDF 作为复核附件，不作检索主源 |
| `split` 数据来源 | `corpus` 代表性样本 / `edge_cases` 边界压力样本 / `uploads` 用户上传 | 目录结构 |

> 数据来源（corpus / edge_cases）只是采样方式，不承担"这篇讲什么"的语义，因此单独放在 `split` 列，大类用于主题领域。

## 二、切片规则

- 按 Markdown 标题切分章节，同一标题下的相邻段落合并到 **450–800 字**，超长段落按句号切分并保留 60 字重叠；
- 章节末尾不足 450 字的收尾段落回并到上一块，避免产生大量无检索价值的碎片；
- 每个切片保留 `heading`（章节路径）、`ordinal`（段落序号）、`fragment`（片段序号），与原文定位一一对应。

## 三、空正文处理

空正文文件仍登记在 `documents` 中并标记为 `empty`，检索时不会编造内容；问答对这类文档的判定结果为"无法判断"。
''', encoding='utf-8')
    print(f'Built {DB}: {counters}')


def rebuild_fts():
    """Rebuild the BM25 index. A plain fts5 table rejects the 'delete-all' command."""
    if not DB.exists():
        raise SystemExit(f'找不到知识库：{DB}')
    con = sqlite3.connect(DB)
    con.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(terms)')
    con.execute('DELETE FROM chunks_fts')
    done = 0
    for cid, heading, text, title in con.execute(
            'SELECT c.id,c.heading,c.text,d.title FROM chunks c '
            'LEFT JOIN documents d ON d.planid=c.planid'):
        con.execute('INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)',
                    (cid, ' '.join(tokenize(f'{heading} {title or ""} {text}'))))
        done += 1
    con.commit()
    con.close()
    print(f'FTS 索引已重建：{done} 条切片')


def backup_db(tag='manual'):
    """生成一致性快照（SQLite 在线备份 API，不受 WAL 影响）。

    先写到系统临时目录再拷回 backups/：目标是新建的库文件，直接建在同步盘/受管目录
    上可能因为建不出 journal 而报 disk I/O error，落地前先在临时目录校验完整性。
    """
    if not DB.exists():
        raise SystemExit(f'找不到知识库：{DB}')
    target_dir = OUTPUT / 'backups'
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = target_dir / f'knowledge_{stamp}_{tag}.sqlite3'
    with tempfile.TemporaryDirectory(prefix='kgsz-backup-') as tmp:
        staged = Path(tmp) / target.name
        src = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        dst = sqlite3.connect(staged)
        with dst:
            src.backup(dst)
        check = dst.execute('PRAGMA integrity_check').fetchone()[0]
        docs = dst.execute('SELECT count(*) FROM documents').fetchone()[0]
        chunks = dst.execute('SELECT count(*) FROM chunks').fetchone()[0]
        dst.close()
        src.close()
        if check != 'ok':
            raise SystemExit(f'备份完整性校验失败：{check}')
        shutil.copyfile(staged, target)
    size = target.stat().st_size / 1e6
    print(f'已备份知识库 → {target}（{size:.1f} MB，integrity ok，{docs} 篇 / {chunks} 切片）')
    return target


def _scan_bad_vectors(con, model):
    """返回 (坏向量数, 样例 id)：全零、NaN/Inf 或长度不是 4 的倍数的 blob。"""
    bad, sample = 0, []
    for ident, blob in con.execute('SELECT id,embedding FROM chunks WHERE embedding IS NOT NULL '
                                   'AND embedding_model=?', (model,)):
        if not blob or len(blob) % 4:
            bad += 1
            if len(sample) < 3:
                sample.append(ident)
            continue
        vec = struct.unpack('<%sf' % (len(blob) // 4), blob)
        norm = 0.0
        broken = False
        for x in vec:
            if x != x or x in (float('inf'), float('-inf')):
                broken = True
                break
            norm += x * x
        if broken or norm <= 0:
            bad += 1
            if len(sample) < 3:
                sample.append(ident)
    return bad, sample


def verify_embeddings(strict=False, probe=False):
    """核对「需要参与语义检索的切片」是否都有当前模型生成的、维度一致且可用的向量。

    只有 documents.status='ready' 的切片会参与检索；空正文文档本来就没有切片。
    probe=True 时还会拿当前端点现算一条向量，验证「库里的向量确实来自这个模型/维度」。
    """
    if not DB.exists():
        raise SystemExit(f'找不到知识库：{DB}')
    model = EMBEDDING_MODEL
    con = sqlite3.connect(DB)
    cols = {r[1] for r in con.execute('PRAGMA table_info(chunks)')}
    if 'embedding' not in cols:
        con.close()
        raise SystemExit('chunks 表没有 embedding 列，尚未做过任何向量回填。')
    total = con.execute('SELECT count(*) FROM chunks').fetchone()[0]
    ready = con.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.planid=c.planid "
                        "WHERE d.status='ready'").fetchone()[0]
    filled = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0]
    matched = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND embedding_model=?',
                          (model,)).fetchone()[0]
    missing = con.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.planid=c.planid "
                          "WHERE d.status='ready' AND (c.embedding IS NULL OR c.embedding_model<>?)",
                          (model,)).fetchone()[0]
    by_model = con.execute("SELECT COALESCE(embedding_model,'(未标注)'),count(*) FROM chunks "
                           "WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
    dims = con.execute('SELECT length(embedding)/4,count(*) FROM chunks WHERE embedding IS NOT NULL '
                       'AND embedding_model=? GROUP BY 1 ORDER BY 2 DESC', (model,)).fetchall()
    bad, bad_sample = _scan_bad_vectors(con, model)
    con.close()

    problems = []
    print(f'库：{DB}')
    print(f'嵌入接口：{embedding_base()}  模型：{model}')
    print(f'切片总数 {total}，参与检索(ready) {ready}')
    print(f'已有向量 {filled}，其中当前模型生成 {matched}，其它模型 {filled - matched}')
    if by_model:
        print('按模型分布：' + '；'.join(f'{m} {n}' for m, n in by_model))
    if dims:
        print('向量维度分布：' + '；'.join(f'{int(d) or 0} 维 × {n}' for d, n in dims))
        if len(dims) > 1:
            problems.append(f'同一模型出现 {len(dims)} 种维度，余弦相似度会整列错位；'
                            f'需 --reset-embeddings 后重跑')
        elif EMBEDDING_DIM and int(dims[0][0]) != EMBEDDING_DIM:
            problems.append(f'库内 {int(dims[0][0])} 维，与 EMBEDDING_DIM={EMBEDDING_DIM} 不符')
    if bad:
        problems.append(f'{bad} 条向量全零/NaN/长度异常（样例 id={bad_sample}）')
    if missing:
        problems.append(f'还差 {missing} 条 ready 切片没有当前模型的向量')

    # 探测与「差多少条」无关：库是空的时候更需要知道钥匙到底通不通。
    # 之前只在 missing==0 时探测，于是 0 向量的库只会说"还差 33039 条"，
    # 却答不出"密钥是不是有效的"——2026-10-27 那次 401 就是因此多绕了一圈。
    if probe:
        started = time.time()
        try:
            vec = embed_texts(['语义检索连通性检查：产业转型升级的重点任务'])[0]
        except RuntimeError as exc:
            problems.append(f'端点探测失败：{exc}')
        else:
            took = (time.time() - started) * 1000
            stored_dim = int(dims[0][0]) if dims else 0
            print(f'端点探测：接口与密钥可用，返回 {len(vec)} 维，耗时 {took:.0f} ms')
            if EMBEDDING_DIM and len(vec) != EMBEDDING_DIM:
                problems.append(f'端点返回 {len(vec)} 维，与 EMBEDDING_DIM={EMBEDDING_DIM} 不符，请改配置')
            elif stored_dim and len(vec) != stored_dim:
                problems.append(f'端点返回 {len(vec)} 维，库里是 {stored_dim} 维：模型或端点已变，'
                                f'需重跑 --index-embeddings')

    if not problems:
        print('结论：ready 切片的向量已全部覆盖，模型与维度一致，未发现坏向量，语义检索可启用。')
        return True
    print('结论：语义检索不会启用（或会退化）。')
    for item in problems:
        print(f'  · {item}')
    print('修复：python3 构建示例知识库.py --index-embeddings（先自动备份，断点续跑）')
    if bad or (dims and len(dims) > 1):
        print('      重跑前建议先清掉不可用的旧向量：python3 构建示例知识库.py --reset-embeddings')
    if strict:
        raise SystemExit(1)
    return False


def reset_embeddings(keep_current=True):
    """清掉非当前模型的向量（或全部），避免换模型后旧 blob 既占空间又制造 stale 噪声。"""
    if not DB.exists():
        raise SystemExit(f'找不到知识库：{DB}')
    con = sqlite3.connect(DB)
    cols = {r[1] for r in con.execute('PRAGMA table_info(chunks)')}
    if 'embedding' not in cols:
        raise SystemExit('chunks 表没有 embedding 列，无需清理。')
    sql = ('UPDATE chunks SET embedding=NULL, embedding_model=NULL'
           + ('' if not keep_current else ' WHERE COALESCE(embedding_model,\'\')<>?'))
    args = () if not keep_current else (EMBEDDING_MODEL,)
    before = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0]
    cur = con.execute(sql, args)
    con.commit()
    after = con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0]
    con.close()
    scope = f'保留 {EMBEDDING_MODEL} 的向量' if keep_current else '全部清空'
    print(f'已清理 {before - after} 条向量（{scope}），剩余 {after} 条。')
    if before - after:
        print('释放的空间需要 VACUUM 才会真正归还：sqlite3 knowledge.sqlite3 "VACUUM;"')


if __name__ == '__main__':
    if '--index-embeddings' in sys.argv:
        require_embedding_config()
        backup_db('before-embeddings')
        backfill_embeddings()
        verify_embeddings(strict=True, probe=True)
    elif '--verify-embeddings' in sys.argv:
        verify_embeddings(strict='--strict' in sys.argv, probe='--probe' in sys.argv)
    elif '--reset-embeddings' in sys.argv:
        backup_db('before-reset')
        reset_embeddings(keep_current='--all' not in sys.argv)
    elif '--backup' in sys.argv:
        backup_db('manual')
    elif '--rebuild-fts' in sys.argv:
        rebuild_fts()
    else:
        build()
