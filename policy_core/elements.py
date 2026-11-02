"""Elements operations for one policy engine instance."""
from urllib.parse import quote

ELEMENT_DIMS=('量化目标','重点产业','政策工具','责任主体')

ELEMENT_RULES={
    '量化目标':(('达到','实现','提高到','增长','控制在','力争','完成','建成'),
               ('%','％','亿元','万元','万人','亿吨','万吨','公里','亩','个','项','倍')),
    '重点产业':(('发展','培育','壮大','建设','打造','重点','加快'),
               ('产业','工业','农业','制造业','旅游','物流','金融','数字经济','园区','基地','集群')),
    '政策工具':(('实行','建立','设立','加大','落实','实施','完善','推行'),
               ('财政','资金','补贴','补助','税收','信贷','贷款','用地','价格','考核','试点','许可','审批','政府采购','转移支付','基金')),
    '责任主体':(('负责','牵头','会同','主体','落实','承担'),
               ('政府','部门','委员会','局','办公室','区县','乡镇','街道','企业','机构','领导小组')),
}


class ElementsMixin:
    def rule_elements(self, sources, per_dim=6):
        """规则抽取四要素：只收原文整句，天然可核对。"""
        rows={d:[] for d in self.ELEMENT_DIMS}
        seen=set()
        for s in sources:
            for sentence in self._sentences(s['text']):
                if len(sentence)>220: sentence=sentence[:220]
                for dim,(verbs,nouns) in self.ELEMENT_RULES.items():
                    if not any(v in sentence for v in verbs): continue
                    if not any(n in sentence for n in nouns): continue
                    key=(dim,sentence)
                    if key in seen: continue
                    seen.add(key)
                    rows[dim].append({'dim':dim,'text':sentence,'type':'原文',
                                      'source_id':s['id'],'filename':s['filename'],
                                      'heading':s['heading'],'fragment':s['fragment'],
                                      'planid':s['planid']})
        for dim in rows: rows[dim]=rows[dim][:per_dim]
        return rows


    def model_elements(self, question, sources):
        """模型抽取，但每条必须附逐字引文；引文不命中即丢弃，不用二次调用复核。"""
        if not self.environment.getenv('LLM_API_KEY','').strip(): return None
        refs='\n\n'.join(f"[{s['id']}] {s['filename']}｜{s['heading']}\n{s['text'][:900]}" for s in sources)
        prompt=('从证据中抽取政策文本四要素：量化目标、重点产业、政策工具、责任主体。'
          '每条给 dim（四要素之一）、point（简短表述）、quote（从证据逐字复制的原文，不得改写）、source_ids。'
          '原文没有明确表述的要素留空数组，禁止推断补全。只输出 JSON：'
          '{"rows":[{"dim":"量化目标","point":"…","quote":"…","source_ids":[1]}],"note":"缺项说明"}。\n'
          f'问题：{question}\n\n证据：\n{refs}')
        try:
            data=self.parse_model_json(self.model_call([{'role':'user','content':prompt}],0))
        except (RuntimeError,ValueError,TypeError):
            return None
        index={s['id']:s for s in sources}
        rows={d:[] for d in self.ELEMENT_DIMS}
        for item in data.get('rows') or []:
            if not isinstance(item,dict): continue
            dim=str(item.get('dim') or '').strip()
            point=str(item.get('point') or '').strip()
            quote=str(item.get('quote') or '').strip()
            ids=item.get('source_ids')
            if dim not in rows or not point or not quote or not isinstance(ids,list) or not ids: continue
            try: ids=list(dict.fromkeys(int(x) for x in ids))
            except (TypeError,ValueError): continue
            if any(i not in index for i in ids): continue
            if self.normalized_text(quote) not in self.normalized_text(''.join(index[i]['text'] for i in ids)): continue
            src=index[ids[0]]
            rows[dim].append({'dim':dim,'text':point,'quote':quote,'type':'原文',
                              'source_id':ids[0],'filename':src['filename'],'heading':src['heading'],
                              'fragment':src['fragment'],'planid':src['planid']})
        if not any(rows.values()): return None
        return rows


    def extract_answer(self, question, sources):
        """返回 (说明文字, 表格行, 状态)。表格行同时供前端渲染与 Excel 导出。"""
        rows=self.model_elements(question,sources)
        status='elements_verified'
        if not rows:
            rows=self.rule_elements(sources)
            status='elements_rule'
        table=[]
        for dim in self.ELEMENT_DIMS:
            for item in rows.get(dim,[]):
                table.append({'dim':dim,'text':item['text'],'quote':item.get('quote') or item['text'],
                              'filename':item['filename'],'heading':item['heading'],
                              'fragment':item['fragment'],'planid':item['planid'],'source_id':item['source_id']})
        missing=[d for d in self.ELEMENT_DIMS if not rows.get(d)]
        lines=[]
        for dim in self.ELEMENT_DIMS:
            items=rows.get(dim) or []
            if not items:
                lines.append(f'{dim}：证据中未见明确表述（不作推断）')
                continue
            lines.append(f'{dim}：')
            for item in items[:6]:
                lines.append(f'  · {item["text"]} [{item["source_id"]}]')
        if missing:
            lines.append('缺项说明：'+'、'.join(missing)+' 在本次检索到的原文中没有直接表述，已留空而非推测。')
        return '\n'.join(lines), table, status


