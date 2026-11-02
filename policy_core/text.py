"""Text normalization, tokenization, periods and document classification."""
from pathlib import Path
import re


STOP_TOKENS = frozenset('''
的 是 和 与 及 在 有 对 为 中 了 这 那 个 我们 可以 需要 请问 一下 如何 怎么 什么 哪些 主要
以及 或者 但是 因为 所以 如果 通过 进行 关于 情况 是否 包括 包含 具有 方面 相关 有关 问题
内容 介绍 说明 简述 概述 谈谈 列出 给出 指出 分析 哪些 怎么样 有没有 请 简要 具体 分别 根据
'''.split())

CN_DIGITS={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}

FIVE_YEAR_START={1:1953,2:1958,3:1966,4:1971,5:1976,6:1981,7:1986,8:1991,9:1996,10:2001,11:2006,12:2011,13:2016,14:2021}

TOPIC_RULES = (
    ('政府综合', ('政府工作报告', '国民经济和社会发展', '民生实事', '政府自身建设')),
    ('农业农村', ('农业', '农村', '乡村', '粮食', '农民', '扶贫', '脱贫', '水利', '畜牧', '渔业', '林业')),
    ('生态环境', ('生态', '环境', '污染', '节能', '减排', '碳', '绿化', '环保', '资源节约', '循环经济')),
    ('科教文卫', ('教育', '科技', '文化', '体育', '人才', '卫生', '医疗', '健康', '计划生育', '广播电视')),
    ('民生保障', ('民政', '养老', '社会保障', '就业', '住房', '人口', '残疾', '退役军人', '妇女儿童', '收入分配')),
    ('基础设施', ('交通', '公路', '铁路', '机场', '港口', '城市', '城镇', '能源', '电力', '供水', '管网', '国土', '防灾')),
    ('经济产业', ('产业', '工业', '经济', '制造', '开发区', '园区', '招商', '商贸', '物流', '金融', '旅游', '数字经济', '国有企业')),
)

def normalized_text(value):
    return re.sub(r'[\s，。、“”"\'《》〈〉（）()：:；;]', '', str(value)).casefold()

def is_existence_question(question):
    return bool(re.search(r'(?:是否有|是否存在|有没有|有无|是否包含|包含.*吗|存在.*吗|有.*吗)', question))

def tokens(s):
    # Chinese character bigrams plus English/numeric words offer a dependency-free baseline.
    s=s.lower(); out=set(re.findall(r'[a-z0-9][a-z0-9._-]*',s))
    for run in re.findall(r'[\u4e00-\u9fff]+',s):
        if len(run)==1: out.add(run)
        else: out.update(run[i:i+2] for i in range(len(run)-1))
    # Drop question scaffolding so generic wording does not dominate BM25 ranking.
    return {t for t in out if t not in STOP_TOKENS}

def _cn_number(value):
    if not value: return 0
    if value=='十': return 10
    if '十' in value:
        head,_,tail=value.partition('十')
        return (CN_DIGITS.get(head,1) if head else 1)*10+CN_DIGITS.get(tail,0)
    return CN_DIGITS.get(value,0)

def period_info(fyp_name, title):
    """返回 (起始年, 展示标签)。规划期按起始年排序，政府工作报告按年份排序。"""
    fyp_name=fyp_name or ''
    match=re.search(r'第([一二三四五六七八九十]+)个?五?年(?:规划|计划)',fyp_name)
    if match:
        index=_cn_number(match.group(1))
        if index:
            # 一五到二五连续，三五起因为 1963–1965 的调整期整体后移三年。
            start=FIVE_YEAR_START.get(index, 1953+5*(index-1)+(3 if index>=3 else 0))
            return start, fyp_name
    if '政府工作报告' in fyp_name:
        year=re.search(r'(?:19|20)\d{2}',title or '')
        return (int(year.group(0)) if year else 9999), '政府工作报告'
    year=re.search(r'(?:19|20)\d{2}',title or '')
    return (int(year.group(0)) if year else 9998), fyp_name or '未标注规划期'

def _clip(text, limit):
    text=re.sub(r'\s+',' ',str(text or '')).strip()
    return text if len(text)<=limit else text[:limit].rstrip()+'…'

def _sentences(text):
    parts=re.split(r'(?<=[。；;！？\n])',text)
    return [re.sub(r'\s+',' ',p).strip(' 。；;\n') for p in parts if len(p.strip())>=12]

def topic_of(title, sample):
    """与构建脚本 topic_of 同规则，保证上传文件与数据集文档的分类口径一致。"""
    title_text=title or ''
    body_text=(sample or '')[:1500]
    best, best_score='其他', 0
    for topic, keys in TOPIC_RULES:
        score=0
        for key in keys:
            if key in title_text: score+=3
            score+=body_text.count(key)/8
        if score>best_score: best, best_score=topic, score
    return best

def classify_document(filename, sample):
    text=filename+' '+sample
    year=re.search(r'(?:19|20)\d{2}',filename+' '+sample[:500])
    region_text=re.sub(r'(?:19|20)\d{2}年?', '', filename+' '+sample[:1500])
    region=re.search(r'([一-鿿]{2,6}(?:省|市|自治区|特别行政区))',region_text)
    kinds=[('政府工作报告','政府工作报告'),('规划','规划'),('实施方案','实施方案'),('行动计划','行动计划'),('意见','意见'),('通知','通知'),('办法','办法')]
    kind=next((label for key,label in kinds if key in text),'其他/未识别')
    return {'year':year.group(0) if year else '年份未识别','region':region.group(1) if region else '区域未识别',
            'kind':kind,'topic':topic_of(Path(filename).stem, sample),}
