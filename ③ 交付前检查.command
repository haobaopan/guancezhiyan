#!/bin/zsh
# ③ 交付前检查.command
# 交付前跑一次：确认包里干净（无密钥/日志/备份）、完整（该有的文档都在）、可用（库没坏）。
# 默认只检查不删除；加 --clean 才会清掉本机运行文件（不含知识库与任何交付文档）。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

CLEAN=0
if [[ "${1:-}" == "--clean" ]]; then CLEAN=1; fi

print "================================================================"
print "  交付前检查（$(date '+%F %T')）"
if (( CLEAN )); then
  print "  模式：检查 + 清理（会删除本机运行文件）"
else
  print "  模式：只检查（不删除任何东西）"
fi
print "================================================================"
print ""

CLEAN="$CLEAN" /usr/bin/env python3 - <<'PY'
import os, re, sys, sqlite3, ast, glob

clean = os.environ.get('CLEAN') == '1'
root = os.getcwd()
ok, warn, bad = [], [], []

def add(bucket, text):
    bucket.append(text)

# ---------- 1. 知识库 ----------
db = os.path.join(root, 'knowledge.sqlite3')
if not os.path.exists(db):
    add(bad, '找不到 knowledge.sqlite3（整个资料库就是它）')
else:
    size = os.path.getsize(db) / 1048576
    try:
        con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        integ = con.execute('pragma integrity_check').fetchone()[0]
        docs = con.execute('select count(*) from documents').fetchone()[0]
        chunks = con.execute('select count(*) from chunks').fetchone()[0]
        fts = con.execute('select count(*) from chunks_fts').fetchone()[0]
        emb = con.execute('select count(*) from chunks where embedding is not null').fetchone()[0]
        models = con.execute('select embedding_model,count(*),min(length(embedding)) from chunks where embedding is not null group by 1').fetchall()
        con.close()
        if integ != 'ok':
            add(bad, f'库完整性检查未通过：{integ}')
        else:
            add(ok, f'库完整：{docs} 篇 / {chunks} 切片 / {fts} 索引 / {emb} 条向量 / {size:.1f} MB')
        if chunks != fts:
            add(warn, f'切片 {chunks} 与全文索引 {fts} 不一致（差 {chunks-fts}），可能导致检索缺漏')
        for m, n, ln in models:
            if ln % 4:
                add(bad, f'向量长度异常：模型 {m} 有 {n} 条，最短 {ln} 字节（不是 4 的倍数）')
            else:
                add(ok, f'向量：{m} × {n} 条，{ln//4} 维')
    except sqlite3.DatabaseError as exc:
        add(bad, f'库无法打开：{exc}')

for suffix in ('-wal', '-shm', '-journal'):
    if os.path.exists(db + suffix):
        add(warn, f'存在 {os.path.basename(db)+suffix}：说明上次不是干净退出，交付前应删除')

# ---------- 2. 运行痕迹（本机文件，不该进交付包） ----------
runtimes = []
for name in ('.env', 'audit.jsonl', 'documents', '配置日志', 'backups'):
    p = os.path.join(root, name)
    if os.path.exists(p):
        runtimes.append(name)
for pat in ('*.bak-*', '.DS_Store', '*.log'):
    for p in glob.glob(os.path.join(root, pat)):
        runtimes.append(os.path.basename(p))

if runtimes:
    if clean:
        removed, failed = [], []
        for name in runtimes:
            p = os.path.join(root, name)
            try:
                if os.path.isdir(p):
                    import shutil; shutil.rmtree(p)
                else:
                    os.remove(p)
                removed.append(name)
            except OSError as exc:
                failed.append(f'{name}（{exc.strerror}）')
        if removed:
            add(ok, '已清理运行文件：' + '、'.join(removed))
        for f in failed:
            add(bad, f'清理失败：{f}')
    else:
        add(bad, '包里有本机运行文件（' + '、'.join(runtimes) + '），交付前必须删除；'
                 '其中 .env 含真实密钥。重跑本脚本加 --clean 可自动清掉')
else:
    add(ok, '无运行痕迹（无 .env / 日志 / 备份 / 临时文件）')

# ---------- 3. 密钥泄漏扫描 ----------
key_re = re.compile(r'\bsk-[A-Za-z0-9_\-]{35,}')
hits = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in ('.git', 'documents', '__pycache__')]
    for fn in filenames:
        if not fn.lower().endswith(('.py', '.md', '.command', '.html', '.json', '.txt', '.csv')):
            continue
        p = os.path.join(dirpath, fn)
        try:
            if os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            text = open(p, encoding='utf-8', errors='ignore').read()
        except OSError:
            continue
        for m in key_re.finditer(text):
            hits.append(f'{os.path.relpath(p, root)}（…{m.group(0)[-6:]}）')
if hits:
    add(bad, '发现疑似真实密钥：' + '；'.join(hits[:6]))
else:
    add(ok, '未在任何文档/源码里发现真实密钥')

# ---------- 4. 交付物齐全性 ----------
need = {
    '应用源码_示例知识库版.py': '应用源码',
    '部署与运维.md': '部署文档 / 运维手册',
    '测试报告.md': '测试报告',
    '答辩材料说明.md': '答辩材料',
    '构建示例知识库.py': '建库与回填工具',
    '验收自检.py': '端到端自检',
    'README.md': '总览',
}
for fn, label in need.items():
    if os.path.exists(os.path.join(root, fn)):
        add(ok, f'{label}：{fn}')
    else:
        add(bad, f'缺少{label}：{fn}')

videos = [f for f in os.listdir(root) if f.lower().endswith(('.mp4', '.mov', '.m4v'))]
if videos:
    add(ok, '演示视频：' + '、'.join(videos))
else:
    add(bad, '缺少演示视频（赛题第（5）项明确要求交付；分镜见 演示视频脚本.md）')

# ---------- 5. 源码可解析 ----------
src = os.path.join(root, '应用源码_示例知识库版.py')
if os.path.exists(src):
    try:
        raw = open(src, encoding='utf-8').read()
        ast.parse(raw)
        add(ok, f'源码语法正确（{len(raw)/1024:.0f} KB）')
        core = os.path.join(root, 'policy_core')
        for module in glob.glob(os.path.join(core, '*.py')):
            with open(module, encoding='utf-8') as handle:
                ast.parse(handle.read(), filename=module)
        for name in ('index.html', 'style.css', 'app.js', 'token_gate.html', 'preview.css', 'preview.js'):
            asset = os.path.join(core, 'web', name)
            if not os.path.isfile(asset):
                raise FileNotFoundError(asset)
        add(ok, '模块化核心与独立前端资源齐全（policy_core/）')
    except Exception as exc:
        add(bad, f'源码无法解析：{exc}')

print('【通过】')
for x in ok: print('  ✓ ' + x)
if warn:
    print('\n【注意】')
    for x in warn: print('  ! ' + x)
if bad:
    print('\n【待处理】')
    for x in bad: print('  ✗ ' + x)

print('\n' + '=' * 64)
if bad:
    print(f'结论：还不能交付 —— {len(bad)} 项待处理' + ('（其中运行文件可用 --clean 自动清理）' if not clean else ''))
    sys.exit(1)
print('结论：可以交付 —— 库完整、包干净、文档齐全')
PY
RC=$?

print ""
if (( RC == 0 )); then
  print "✅ 结论：可以交付。"
  print "   下一步：把整个「管策智研10.28版」文件夹压缩打包，或按要求上传。"
else
  print "❌ 结论：还不能交付（详见上方【待处理】）。"
  print "   只想去掉运行痕迹：双击本文件时加参数，或在终端执行 ./③\\ 交付前检查.command --clean"
fi
print ""
