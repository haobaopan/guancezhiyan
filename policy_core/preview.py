"""Preview operations for one policy engine instance."""
from pathlib import Path
from urllib.parse import quote
import json
import re


class PreviewMixin:
    def _preview_bar(self, row, chunk, filename):
        bits=[]
        if chunk is not None:
            bits.append(f"<span class='kgsz-tag'>章节 {self.xml_escape(chunk['heading'])}</span>")
            bits.append(f"<span class='kgsz-tag'>段落 {chunk['ordinal']}</span>")
            bits.append(f"<span class='kgsz-tag'>片段 {chunk['fragment']}</span>")
        dl=f"/source/{quote(row['planid'])}?dl=1"
        return ('<div class="kgsz-bar"><b>原文回看</b>'
                f"<span>{self.xml_escape(filename or row['title'])}</span>{''.join(bits)}"
                f"<a href='{dl}'>下载原始文件</a>"
                "<a href='javascript:history.back()'>返回</a></div>")


    def preview_page(self, row, chunk, body='', needle='', filename='', notice=''):
        """Standalone reader page used when no formatted HTML exists for the document."""
        inner=notice or body or ''
        payload=json.dumps(needle or '', ensure_ascii=False).replace('</','<\\/')
        return ('<!doctype html><html lang="zh"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>原文｜{self.xml_escape(filename or row["title"])}</title>'
                + self._PREVIEW_STYLE + '<body class="kgsz-body">'
                + self._preview_bar(row, chunk, filename)
                + f'<div class="kgsz-paper" id="kgsz-doc">{inner}</div>'
                + f'<script>window.__KGSZ_NEEDLE__={payload};</script>' + self._PREVIEW_SCRIPT
                + '</body></html>')


    def with_highlight(self, doc_html, row, chunk, needle, filename):
        """Inject the citation bar and fragment highlighter into the dataset's L2 HTML."""
        payload=json.dumps(needle or '', ensure_ascii=False).replace('</','<\\/')
        doc=doc_html
        low=doc.lower()
        # 数据集原始 HTML 多是桌面排版，通常没有 viewport。手机打开时会按 980px 排版，
        # 字小到看不清、点击也点不准。这里补一条，让 /source/ 在手机上是正常宽度。
        vp='<meta name="viewport" content="width=device-width,initial-scale=1">'
        has_vp=('name="viewport"' in low) or ("name='viewport'" in low)
        if '</head>' in low:
            i=low.rindex('</head>')
            doc=doc[:i]+(('' if has_vp else vp)+self._PREVIEW_STYLE)+doc[i:]
        else:
            doc=(('' if has_vp else vp)+self._PREVIEW_STYLE)+doc
        m=re.search(r'<body[^>]*>',doc,re.I)
        bar=self._preview_bar(row,chunk,filename)
        doc=(doc[:m.end()]+bar+doc[m.end():]) if m else (bar+doc)
        tail=f'<script>window.__KGSZ_NEEDLE__={payload};</script>'+self._PREVIEW_SCRIPT
        low=doc.lower()
        if '</body>' in low:
            i=low.rindex('</body>'); doc=doc[:i]+tail+doc[i:]
        else:
            doc+=tail
        return doc


    def find_preview_source(self, row, target):
        """'html' | 'md' | 'file' — prefer the dataset's L2 HTML for in-browser reading."""
        candidates=[target] if target.suffix.lower() in ('.html','.htm') else []
        if row['split'] in ('corpus','edge_cases') and self.DATASET_ROOT:
            candidates.append((self.DATASET_ROOT/Path(row['source_path'])).with_suffix('.html'))
            # Also locate by planid so the L2 HTML still resolves after the original
            # file was migrated into the local originals/ store.
            candidates.append(self.DATASET_ROOT/row['split']/str(row['planid'])/f'{row["planid"]}.html')
        candidates.append(target.with_suffix('.html'))
        for p in candidates:
            if p.is_file(): return 'html',p
        if target.suffix.lower() in ('.md','.markdown','.txt'): return 'md',target
        return 'file',target


    def md_to_html(self, text):
        """Small Markdown renderer (headings / lists / paragraphs) for source preview."""
        body=text
        if body.startswith('---'):
            end=body.find('\n---',3)
            if end>=0: body=body[end+4:]
        out=[]; para=[]
        def flush():
            if para:
                out.append('<p>'+'<br>'.join(self.xml_escape(x) for x in para)+'</p>'); para.clear()
        for raw in body.replace('\r','').splitlines():
            line=raw.strip()
            if not line: flush(); continue
            m=re.match(r'^(#{1,6})\s+(.*)$',line)
            if m:
                flush(); lv=min(6,len(m.group(1)))
                out.append(f'<h{lv}>{self.xml_escape(m.group(2).strip())}</h{lv}>'); continue
            if line.startswith(('- ','* ')): para.append('· '+line[2:]); continue
            para.append(line)
        flush()
        return '\n'.join(out)


