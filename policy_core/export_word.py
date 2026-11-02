"""Word OOXML export."""
from urllib.parse import quote
import zipfile
from .export_xml import xml_escape


def _run(text, bold=False, size=21, color=None):
    rpr='<w:rPr>'+('<w:b/>' if bold else '')+(f'<w:color w:val="{color}"/>' if color else '')+f'<w:sz w:val="{size}"/></w:rPr>'
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'

def _para(runs, style=None, indent=False, jc=None, space_after=None):
    ppr='<w:pPr>'
    if style: ppr+=f'<w:pStyle w:val="{style}"/>'
    if jc: ppr+=f'<w:jc w:val="{jc}"/>'
    if indent: ppr+='<w:ind w:left="420" w:hanging="210"/>'
    if space_after is not None: ppr+=f'<w:spacing w:after="{space_after}"/>'
    ppr+='</w:pPr>'
    return f'<w:p>{ppr}{"".join(runs)}</w:p>'

def _docx_table(header, rows, widths):
    grid=''.join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    borders=('<w:tblBorders>'+''.join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFCBD6"/>'
        for side in ('top','left','bottom','right','insideH','insideV'))+'</w:tblBorders>')
    def cell(text, width, bold=False, shade=False):
        tcpr=f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>'+(f'<w:shd w:val="clear" w:color="auto" w:fill="EAF1F6"/>' if shade else '')+'<w:vAlign w:val="center"/></w:tcPr>'
        return f'<w:tc>{tcpr}{_para([_run(text,bold=bold,size=19)],space_after=0)}</w:tc>'
    out=[f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders/>{borders}<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
    out.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>'+''.join(cell(h,w,True,True) for h,w in zip(header,widths))+'</w:tr>')
    for row in rows:
        out.append('<w:tr>'+''.join(cell(str(v),w) for v,w in zip(row,widths))+'</w:tr>')
    out.append('</w:tbl>')
    out.append(_para([_run('',size=12)],space_after=0))
    return ''.join(out)

def make_docx(title, answer, sources, table=None, base_url=''):
    """生成可直接提交的 Word：页码、目录式标题、引用超链接、四要素表格。

    检索式原型只输出若干段落，评审最常问的就是"页码呢、引用点得开吗"；
    这里用纯 OOXML 补齐页脚域、超链接关系与表格样式，不依赖第三方库。
    """
    import io
    from datetime import datetime
    rels=[]; hyperlink_rid={}
    def link_rid(url):
        if url not in hyperlink_rid:
            rid=f'rIdL{len(hyperlink_rid)+1}'
            hyperlink_rid[url]=rid
            rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{xml_escape(url)}" TargetMode="External"/>')
        return hyperlink_rid[url]
    body=[_para([_run(title,size=40)],style='Title')]
    body.append(_para([_run(f'管策智研 · 政策文本研究助手　生成时间 {datetime.now().strftime("%Y-%m-%d %H:%M")}',size=18,color='6B7A8C')],style='Subtitle'))
    for line in answer.splitlines():
        raw=line.rstrip()
        if not raw.strip(): continue
        stripped=raw.strip()
        if stripped.startswith('【') and stripped.endswith('】') or stripped.startswith('【'):
            body.append(_para([_run(stripped,size=28)],style='Heading2'))
        elif stripped.startswith('·'):
            body.append(_para([_run(stripped,size=21)],indent=True))
        elif stripped.startswith('#'):
            body.append(_para([_run(stripped.lstrip('# '),size=26)],style='Heading2'))
        else:
            body.append(_para([_run(stripped,size=21)],indent=True))
    if table:
        body.append(_para([_run('四要素对照表',size=28)],style='Heading1'))
        header=['维度','要素表述','出处（文件 · 章节 · 片段）','原文引文']
        rows=[[r['dim'],r['text'],f"{r['filename']}｜{r['heading']}｜片段 {r['fragment']}",r.get('quote') or r['text']] for r in table]
        body.append(_docx_table(header,rows,[900,3000,2600,2500]))
    body.append(_para([_run('引用原文与定位',size=28)],style='Heading1'))
    for s in sources:
        loc=f"[{s['id']}] {s['filename']}｜{s['heading']}｜段落 {s['ordinal']}｜片段 {s['fragment']}"
        runs=[_run(loc,bold=True,size=20)]
        if base_url:
            url=f"{base_url.rstrip('/')}/source/{quote(str(s['planid']))}?frag={s['fragment']}"
            runs.append(_run('　',size=20))
            runs.append(f'<w:hyperlink r:id="{link_rid(url)}"><w:r><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/><w:sz w:val="20"/></w:rPr><w:t>打开原文该片段</w:t></w:r></w:hyperlink>')
        body.append(_para(runs))
        body.append(_para([_run(s['text'],size=20,color='333333')],indent=True))
    body.append(_para([_run('说明：以上结论仅依据所列原文片段，未通过证据校验的内容已省略；引用编号对应文末“引用原文与定位”。',size=18,color='6B7A8C')]))
    sect=('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1440" w:right="1418" w:bottom="1440" w:left="1418" w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')
    document=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
        +''.join(body)+sect+'</w:body></w:document>')
    styles=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman"/>'
        '<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
        '<w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="120"/></w:pPr>'
        '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:jc w:val="center"/><w:spacing w:after="240"/></w:pPr><w:rPr><w:color w:val="6B7A8C"/><w:sz w:val="18"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>'
        '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr>'
        '<w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
        '</w:styles>')
    footer=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:sz w:val="18"/><w:color w:val="777777"/></w:rPr><w:t>第 </w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>'
        '<w:r><w:rPr><w:sz w:val="18"/><w:color w:val="777777"/></w:rPr><w:t> 页 / 共 </w:t></w:r>'
        '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>'
        '<w:r><w:rPr><w:sz w:val="18"/><w:color w:val="777777"/></w:rPr><w:t> 页　管策智研</w:t></w:r>'
        '</w:p></w:ftr>')
    rel_list=['<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
              '<Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>']+rels
    content=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        '</Types>')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',content)
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml',document)
        z.writestr('word/styles.xml',styles)
        z.writestr('word/footer1.xml',footer)
        z.writestr('word/_rels/document.xml.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(rel_list)+'</Relationships>')
    return out.getvalue()
