# -*- coding: utf-8 -*-
"""检索命中率评测：用官方训练集的标注 planid 复核知识库检索质量。

用法：
    python3 评测脚本.py                     # 全量评测
    python3 评测脚本.py --limit 50           # 先跑 50 题试水
    python3 评测脚本.py --k 8 --out 报告前缀  # 指定 top-k 与输出前缀

评测口径（与赛题验收指标"检索命中率不低于 75%"对齐）：
    命中 = 该题 top-k 检索结果中，至少有一条来自标注的 expected_planids。
    同时给出"完全覆盖"（全部标注文档都进 top-k）与首条命中排名分布。

数据来源：
    train.jsonl        题目（qid / stem / turns / cat / fmt）
    训练集/*.jsonl      逐题元数据（expected_planids，可能是多条 meta 记录，按 qid 合并）
两份文件按 qid 关联；只有能在题库里找到 planid 标注的题目才纳入统计。
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module():
    """直接加载应用模块，确保评测走的是真实检索链路而不是复刻实现。"""
    spec = importlib.util.spec_from_file_location('kgsz_app', HERE / '应用源码_示例知识库版.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['kgsz_app'] = module
    spec.loader.exec_module(module)
    return module


def load_questions(train_jsonl: Path, meta_dir: Path):
    questions = []
    for raw in train_jsonl.read_text(encoding='utf-8').splitlines():
        raw = raw.strip()
        if raw:
            questions.append(json.loads(raw))
    expected = {}
    for path in sorted(meta_dir.glob('*.jsonl')):
        for raw in path.read_text(encoding='utf-8').splitlines():
            raw = raw.strip()
            if not raw:
                continue
            item = json.loads(raw)
            planids = item.get('expected_planids') or []
            if not planids:
                continue
            expected.setdefault(item.get('qid'), set()).update(str(x) for x in planids)
    return questions, expected


def first_question(item):
    turns = item.get('turns') or []
    for turn in turns:
        if str(turn.get('content', '')).strip():
            return str(turn['content']).strip()
    stem = str(item.get('stem') or '')
    # 多轮题的 stem 常见格式为“第1轮：…”，取第一轮作为检索输入。
    for marker in ('第1轮：', '第1轮:'):
        if marker in stem:
            return stem.split(marker, 1)[1].split('第2轮')[0].strip()
    return stem.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--train', default=str(Path.home() / 'Desktop' / '管策智研检验所用数据' /
                                              '管策智研_训练集_v1.0' / 'train.jsonl'))
    parser.add_argument('--meta', default=str(Path.home() / 'Desktop' / '管策智研检验所用数据' /
                                             '管策智研_训练集_v1.0' / '训练集'))
    parser.add_argument('--k', type=int, default=8, help='取前 k 条作为检索结果，默认 8')
    parser.add_argument('--limit', type=int, default=0, help='只评测前 N 题，0 表示全部')
    parser.add_argument('--cat', default='', help='只评测指定大类，如 A / B / E')
    parser.add_argument('--out', default='评测报告_检索', help='输出文件名前缀')
    parser.add_argument('--out-dir', default='', help='输出目录，默认脚本所在目录（复核副本时指到临时目录）')
    args = parser.parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else HERE
    out_dir.mkdir(parents=True, exist_ok=True)

    train_path, meta_dir = Path(args.train), Path(args.meta)
    if not train_path.is_file() or not meta_dir.is_dir():
        sys.exit(f'找不到题库：{train_path} 或 {meta_dir}。请用 --train/--meta 指定路径。')

    app = load_module()
    questions, expected = load_questions(train_path, meta_dir)
    con = app.db()
    have = {r[0] for r in con.execute('SELECT planid FROM documents')}
    con.close()

    rows = []
    for item in questions:
        qid = item.get('qid')
        if qid not in expected:
            continue
        if args.cat and item.get('cat') != args.cat:
            continue
        if not (expected[qid] & have):
            continue  # 标注文档不在当前知识库内，无法判定命中
        question = first_question(item)
        if not question:
            continue
        started = time.perf_counter()
        try:
            hits = app.retrieve(question, limit=args.k)
            error = ''
        except Exception as exc:  # 单题失败不应中断整轮评测
            hits, error = [], f'{type(exc).__name__}: {exc}'
        elapsed = time.perf_counter() - started
        got = [str(h['planid']) for h in hits]
        wanted = expected[qid] & have
        rank = next((i + 1 for i, pid in enumerate(got) if pid in wanted), 0)
        rows.append({
            'qid': qid, 'cat': item.get('cat'), 'sub': item.get('sub'),
            'fmt': item.get('fmt'), 'difficulty': item.get('difficulty'),
            'question': question, 'expected': '|'.join(sorted(wanted)),
            'got': '|'.join(got), 'hit_rank': rank,
            'covered': int(bool(wanted) and wanted.issubset(got)),
            'elapsed_ms': round(elapsed * 1000, 1), 'error': error,
        })
        if args.limit and len(rows) >= args.limit:
            break

    if not rows:
        sys.exit('没有可评测的题目：请检查题库路径或知识库是否已建好索引。')

    total = len(rows)
    hits = [r for r in rows if r['hit_rank'] > 0]
    full = [r for r in rows if r['covered']]
    times = sorted(r['elapsed_ms'] for r in rows)
    p50 = statistics.median(times)
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    hit_rate = len(hits) / total * 100
    full_rate = len(full) / total * 100
    top1 = sum(1 for r in rows if r['hit_rank'] == 1) / total * 100

    by_cat = {}
    for r in rows:
        bucket = by_cat.setdefault(r['cat'] or '?', [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if r['hit_rank'] else 0

    csv_path = out_dir / f'{args.out}.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    report = [
        '# 检索评测报告（自动生成）', '',
        f'- 评测时间：{time.strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 知识库：`{app.DB}`',
        f'- 题库：`{train_path}`（标注目录 `{meta_dir}`）',
        f'- 评测题数：{total}（top-{args.k}）',
        '', '## 指标', '',
        f'| 指标 | 结果 | 赛题要求 |', '|---|---|---|',
        f'| 检索命中率（top-{args.k} 命中标注文档） | **{hit_rate:.1f}%** | ≥75% |',
        f'| 完全覆盖率（标注文档全部进 top-{args.k}） | {full_rate:.1f}% | — |',
        f'| 首条即命中率 | {top1:.1f}% | — |',
        f'| 检索耗时 P50 / P95 | {p50:.0f} ms / {p95:.0f} ms | 端到端 ≤180 s |',
        f'| 异常题数 | {sum(1 for r in rows if r["error"])} | — |',
        '', '## 分类别命中率', '', '| 大类 | 题数 | 命中率 |', '|---|---|---|',
    ]
    for cat in sorted(by_cat):
        n, h = by_cat[cat]
        report.append(f'| {cat} | {n} | {h / n * 100:.1f}% |')
    report += [
        '', '## 口径与待测项', '',
        '- 命中判定只依据题库标注的 `expected_planids`，不含模型生成环节，可完全复现。',
        '- **原文一致率、引用正确率需配置模型后评测**，未配置 `LLM_API_KEY` 时本报告不给出结论，标记为待测。',
        '- 未命中题目明细见同名 CSV，可按 `hit_rank=0` 筛选后逐条分析（多因问题表述与原文用词差异、或标注文档存在多份相似文本）。',
        '', f'明细：`{csv_path.name}`',
    ]
    md_path = out_dir / f'{args.out}.md'
    md_path.write_text('\n'.join(report) + '\n', encoding='utf-8')

    print(f'评测完成：{total} 题｜命中率 {hit_rate:.1f}%｜完全覆盖 {full_rate:.1f}%｜首条命中 {top1:.1f}%')
    print(f'P50 {p50:.0f}ms / P95 {p95:.0f}ms｜异常 {sum(1 for r in rows if r["error"])} 题')
    for cat in sorted(by_cat):
        n, h = by_cat[cat]
        print(f'  {cat}: {n} 题，命中 {h / n * 100:.1f}%')
    print(f'报告：{md_path.name}　明细：{csv_path.name}')


if __name__ == '__main__':
    main()
