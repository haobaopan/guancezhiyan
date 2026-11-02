"""PowerPoint OOXML export."""
import zipfile
from .export_xml import xml_escape


def _emu(inch):
    return int(inch * 914400)

def _pptx_textbox(shape_id, x, y, cx, cy, paragraphs, anchor='t'):
    """paragraphs: [(text, size_pt, bold, color_hex)]"""
    body=''
    for text, size, bold, color in paragraphs:
        bold_attr=' b="1"' if bold else ''
        body+=('<a:p><a:pPr algn="l"><a:lnSpc><a:spcPct val="120000"/></a:lnSpc>'
               '<a:spcBef><a:spcPts val="600"/></a:spcBef></a:pPr>'
               f'<a:r><a:rPr lang="zh-CN" sz="{max(800,int(size*100))}"{bold_attr}>'
               f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
               '<a:latin typeface="微软雅黑"/><a:ea typeface="微软雅黑"/></a:rPr>'
               f'<a:t>{xml_escape(text)}</a:t></a:r></a:p>')
    if not body:
        body='<a:p><a:endParaRPr lang="zh-CN"/></a:p>'
    return ('<p:sp><p:nvSpPr>'
            f'<p:cNvPr id="{shape_id}" name="Shape {shape_id}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{_emu(x)}" y="{_emu(y)}"/><a:ext cx="{_emu(cx)}" cy="{_emu(cy)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" lIns="91440" tIns="45720" rIns="91440" bIns="45720" anchor="{anchor}"><a:normAutofit/></a:bodyPr>'
            f'<a:lstStyle/>{body}</p:txBody></p:sp>')

def _pptx_rect(shape_id, x, y, cx, cy, color):
    return ('<p:sp><p:nvSpPr>'
            f'<p:cNvPr id="{shape_id}" name="Rect {shape_id}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{_emu(x)}" y="{_emu(y)}"/><a:ext cx="{_emu(cx)}" cy="{_emu(cy)}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            '<a:ln><a:noFill/></a:ln></p:spPr></p:sp>')

def _pptx_slide(shapes):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvGrpSpPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        + ''.join(shapes) +
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')

def make_pptx(title, answer, sources, periods=None):
    """多页演示文稿：封面 → 核心结论 → 分期对照（若有）→ 证据清单 → 口径说明。

    单页幻灯片无法承载"格式规范、可直接使用"的要求：答辩时至少要有封面、
    结论、对照和证据四类页面。
    """
    import io
    from datetime import datetime
    NAVY, TEAL, GREY, DARK = '123C68', '176B79', '6B7A8C', '172033'
    slides=[]
    # 1) 封面
    slides.append(_pptx_slide([
        _pptx_rect(2, 0, 0, 13.333, 7.5, NAVY),
        _pptx_rect(3, 0, 5.1, 13.333, 0.06, TEAL),
        _pptx_textbox(4, 0.9, 2.2, 11.5, 1.9, [(title[:70], 34, True, 'FFFFFF')], anchor='b'),
        _pptx_textbox(5, 0.9, 4.25, 11.5, 0.7, [('管策智研 · 政策文本研究助手　基于本地知识库的证据可溯源分析', 15, False, 'CBD9E6')]),
        _pptx_textbox(6, 0.9, 5.5, 11.5, 0.6, [(f'生成时间 {datetime.now().strftime("%Y-%m-%d %H:%M")}　|　引用片段 {len(sources)} 条', 12, False, '9FB3C8')]),
    ]))
    # 2) 核心结论
    lines=[x.strip().lstrip('·').strip() for x in answer.splitlines() if x.strip() and not x.strip().startswith('【')]
    bullets=[(x[:110], 15, False, DARK) for x in lines[:8]] or [('（本次未生成可校验结论，仅提供原文证据）', 15, False, GREY)]
    slides.append(_pptx_slide([
        _pptx_rect(2, 0, 0, 0.22, 7.5, TEAL),
        _pptx_textbox(3, 0.55, 0.45, 12.2, 0.8, [('核心结论', 26, True, NAVY)]),
        _pptx_textbox(4, 0.55, 1.35, 12.2, 5.6, bullets),
    ]))
    # 3) 分期对照（跨时期演变时使用）
    if periods:
        rows=[]
        for p in periods[:6]:
            head=f'{p["label"]}（{p["doc_count"]} 份）'
            detail='；'.join(x['text'][:60] for x in p.get('points',[])[:2]) or '本期无直接对应表述'
            rows.append((head, 15, True, NAVY))
            rows.append((detail, 12, False, DARK))
        slides.append(_pptx_slide([
            _pptx_rect(2, 0, 0, 0.22, 7.5, TEAL),
            _pptx_textbox(3, 0.55, 0.45, 12.2, 0.8, [('按规划期对照', 26, True, NAVY)]),
            _pptx_textbox(4, 0.55, 1.3, 12.2, 5.7, rows),
        ]))
    # 4) 证据清单
    ev=[(f'[{s["id"]}] {s["filename"]}｜{s["heading"]}｜片段 {s["fragment"]}', 12, True, TEAL) for s in sources[:6]]
    ev+=[(x['text'][:80], 10.5, False, GREY) for x in sources[:1]]
    slides.append(_pptx_slide([
        _pptx_rect(2, 0, 0, 0.22, 7.5, TEAL),
        _pptx_textbox(3, 0.55, 0.45, 12.2, 0.8, [('证据与定位', 26, True, NAVY)]),
        _pptx_textbox(4, 0.55, 1.3, 12.2, 5.7, ev),
    ]))
    # 5) 口径说明
    slides.append(_pptx_slide([
        _pptx_rect(2, 0, 0, 0.22, 7.5, TEAL),
        _pptx_textbox(3, 0.55, 0.45, 12.2, 0.8, [('口径与边界', 26, True, NAVY)]),
        _pptx_textbox(4, 0.55, 1.3, 12.2, 5.6, [
            ('· 所有结论只依据列出的原文片段，未通过证据校验的主张已省略。', 14, False, DARK),
            ('· 每个片段可用"文件名 · 章节 · 段落 · 片段编号"回到原文核对。', 14, False, DARK),
            ('· 知识库未收录的内容不作推断；检索不到时明确回答"未找到"。', 14, False, DARK),
            ('· 规划期口径以数据集元数据 fyp_name 为准，跨期对比按起始年排序。', 14, False, DARK),
        ]),
    ]))
    n=len(slides)
    types=('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        + ''.join(f'<Override PartName="/ppt/slides/slide{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for i in range(n)) +
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>')
    sld_ids=''.join(f'<p:sldId id="{256+i}" r:id="rId{i+1}"/>' for i in range(n))
    pres=('<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId{n+1}"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{sld_ids}</p:sldIdLst>'
        '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    rootrels=('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
    prels=('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i+1}.xml"/>' for i in range(n))
        + f'<Relationship Id="rId{n+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        + '</Relationships>')
    srels=('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>')
    layout=('<?xml version="1.0" encoding="UTF-8"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
        '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvGrpSpPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    master=('<?xml version="1.0" encoding="UTF-8"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld name="Master"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvGrpSpPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="4F81BD" accent2="C0504D" accent3="9BBB59" accent4="8064A2" accent5="4BACC6" accent6="F79646" hlink="0000FF" folHlink="800080"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>')
    masterrels=('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>')
    theme=('<?xml version="1.0" encoding="UTF-8"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">'
        '<a:themeElements><a:clrScheme name="Office"><a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="176B79"/></a:accent1>'
        '<a:accent2><a:srgbClr val="C0504D"/></a:accent2><a:accent3><a:srgbClr val="70AD47"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4>'
        '<a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
        '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Office">'
        '<a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface="微软雅黑"/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Aptos"/><a:ea typeface="微软雅黑"/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
        '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>'
        '</a:themeElements></a:theme>')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',types)
        z.writestr('_rels/.rels',rootrels)
        z.writestr('ppt/presentation.xml',pres)
        z.writestr('ppt/_rels/presentation.xml.rels',prels)
        for i,slide in enumerate(slides,1):
            z.writestr(f'ppt/slides/slide{i}.xml',slide)
            z.writestr(f'ppt/slides/_rels/slide{i}.xml.rels',srels)
        z.writestr('ppt/slideLayouts/slideLayout1.xml',layout)
        z.writestr('ppt/slideLayouts/_rels/slideLayout1.xml.rels',masterrels)
        z.writestr('ppt/slideMasters/slideMaster1.xml',master)
        z.writestr('ppt/slideMasters/_rels/slideMaster1.xml.rels',masterrels)
        z.writestr('ppt/theme/theme1.xml',theme)
    return out.getvalue()
