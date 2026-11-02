"""Excel OOXML export."""
import zipfile
from .export_xml import xml_escape


def make_xlsx(header, rows, sheet_name='四要素', widths=(10,40,34,40)):
    """生成真正的 .xlsx（Excel 可直接打开、可继续编辑），非 CSV 改名。"""
    import io
    def col(i):
        name=''; i+=1
        while i: i,rem=divmod(i-1,26); name=chr(65+rem)+name
        return name
    def cell(ref, text, style):
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(str(text))}</t></is></c>'
    sheet=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetPr><tabColor rgb="FF176B79"/></sheetPr>'
           f'<dimension ref="A1:{col(len(header)-1)}{len(rows)+1}"/>'
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
           '<sheetFormatPr defaultRowHeight="18"/>'
           '<cols>'+''.join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths))+'</cols>'
           '<sheetData>']
    sheet.append('<row r="1" ht="26" customHeight="1">'+''.join(cell(f'{col(i)}1',h,1) for i,h in enumerate(header))+'</row>')
    for r,row in enumerate(rows,2):
        sheet.append(f'<row r="{r}">'+''.join(cell(f'{col(i)}{r}',v,2) for i,v in enumerate(row))+'</row>')
    sheet.append('</sheetData>'
                 f'<autoFilter ref="A1:{col(len(header)-1)}{len(rows)+1}"/>'
                 '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
                 '</worksheet>')
    workbook=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{xml_escape(sheet_name[:28])}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    styles=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="宋体"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="微软雅黑"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF176B79"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border/><border><left style="thin"><color rgb="FFBFCBD6"/></left><right style="thin"><color rgb="FFBFCBD6"/></right>'
        '<top style="thin"><color rgb="FFBFCBD6"/></top><bottom style="thin"><color rgb="FFBFCBD6"/></bottom></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFill="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    content=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',content)
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',workbook)
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',''.join(sheet))
        z.writestr('xl/styles.xml',styles)
    return out.getvalue()
