"""Documents operations for one policy engine instance."""
from pathlib import Path
import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile


class DocumentsMixin:
    def ensure_dir(self, path):
        """建目录；已存在、或所在位置不允许创建但目录本来就在，都不算错误。"""
        try:
            path.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, PermissionError):
            if not path.is_dir():
                raise
        return path


    def extract_text(self, name, data):
        suffix = Path(name).suffix.lower()
        if suffix in ('.txt', '.md', '.markdown', '.csv'):
            return data.decode('utf-8-sig', errors='replace')
        if suffix == '.docx':
            with zipfile.ZipFile(__import__('io').BytesIO(data)) as z:
                # Guard against decompression bombs before reading any member.
                if sum(i.file_size for i in z.infolist()) > self.MAX_UNZIPPED: raise ValueError('文档解压后体积过大，已拒绝处理。')
                root = ET.fromstring(z.read('word/document.xml'))
            ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            out=[]
            for p in root.findall('.//w:p', ns):
                s=''.join(t.text or '' for t in p.findall('.//w:t', ns)).strip()
                if s: out.append(s)
            return '\n'.join(out)
        if suffix == '.pdf':
            # Optional system utility; never OCRs scanned documents.
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
                f.write(data); f.flush()
                p=subprocess.run(['pdftotext','-layout',f.name,'-'],capture_output=True,timeout=20)
                if p.returncode: raise ValueError('PDF文字提取失败；请确认文件是可复制文字的PDF。')
                return p.stdout.decode('utf-8', errors='replace')
        raise ValueError('支持 .txt、.md、.docx 和可复制文字的 .pdf 文件。')


    def _paragraphs(self, text):
        """按标题层级把正文拆成 (标题路径, 段落) 序列，保留原始段落边界。"""
        body=text
        if body.startswith('---'):
            end=body.find('\n---',3)
            if end>=0: body=body[end+4:]
        headings=[]; paragraphs=[]; buffer=[]
        def flush():
            if buffer:
                value=re.sub(r'\s+',' ',' '.join(buffer)).strip()
                if value: paragraphs.append((' / '.join(headings) or '正文',value))
                buffer.clear()
        for raw in body.replace('\r','').splitlines():
            line=raw.strip(); md=re.match(r'^(#{1,6})\s+(.+?)\s*$',line)
            numbered=re.match(r'^(第[一二三四五六七八九十百0-9]+[章节篇部分]|[一二三四五六七八九十]+、|\d+(?:\.\d+)*\s+)(.+)$',line)
            if md or (numbered and len(line)<100):
                flush(); match=md or numbered; title=match.group(2).strip(); level=len(md.group(1)) if md else 1
                headings[:]=headings[:level-1]; headings.append(title)
            elif line: buffer.append(line)
            else: flush()
        flush()
        return paragraphs


    def split_chunks(self, filename, text):
        """把正文切成 450–800 字的语义块。

    早期实现按"一个自然段一个切片"输出，导致 65% 的切片不足 200 字：既让
    BM25 的文档长度归一化失真，也让模型拿到的证据缺少上下文。现在同一标题
    下的相邻段落先合并到目标长度，只有超长段落才按句号切分并保留重叠。
    """
        paragraphs=self._paragraphs(text)
        blocks=[]; index=0
        while index<len(paragraphs):
            heading,block=paragraphs[index]; ordinal=index+1
            nxt=index+1
            while nxt<len(paragraphs) and paragraphs[nxt][0]==heading and len(block)<self.CHUNK_MIN:
                merged=block+'\n'+paragraphs[nxt][1]
                if len(merged)>self.CHUNK_MAX: break
                block=merged; nxt+=1
            if len(block)<=self.CHUNK_MAX:
                blocks.append((heading,ordinal,block))
            else:
                start=0
                while start<len(block):
                    end=min(len(block),start+self.CHUNK_MAX)
                    if end<len(block):
                        boundary=max(block.rfind(x,start+self.CHUNK_MIN//2,end) for x in '。！？；\n')
                        if boundary>start: end=boundary+1
                    piece=block[start:end].strip()
                    if piece: blocks.append((heading,ordinal,piece))
                    if end>=len(block): break
                    start=max(end-self.CHUNK_OVERLAP,start+1)
            index=nxt
        # 章节末尾常剩一段几十字的收尾语，单独成块几乎没有检索价值，回并到上一块。
        merged_blocks=[]
        for heading,ordinal,piece in blocks:
            if (merged_blocks and len(piece)<self.CHUNK_MIN
                    and merged_blocks[-1][0]==heading
                    and len(merged_blocks[-1][2])+len(piece)+1<=self.CHUNK_MAX):
                ph,po,pt=merged_blocks[-1]; merged_blocks[-1]=(ph,po,pt+'\n'+piece)
            else:
                merged_blocks.append((heading,ordinal,piece))
        return [(filename,h,o,i+1,t) for i,(h,o,t) in enumerate(merged_blocks)]


    def index_file(self, name, data):
        if len(data)>self.MAX_UPLOAD: raise ValueError('单个文件不能超过20MB。')
        text=self.extract_text(name,data)
        chunks=self.split_chunks(Path(name).name,text)
        if not chunks: raise ValueError('未提取到正文内容。')
        filename=Path(name).name
        digest=hashlib.sha256(data).hexdigest()
        # A document identity includes both content and original name, so same-name
        # files with different contents remain separate records and exact repeats dedupe.
        planid='upload:'+hashlib.sha256(data+b'\0'+filename.encode('utf-8')).hexdigest()
        chunk_filename=digest[:16]+'_'+filename
        title=Path(filename).stem
        suffix=Path(filename).suffix.lower().lstrip('.') or 'unknown'
        metadata=self.classify_document(filename, text[:1500])
        # Content-addressed storage avoids overwrites and stores each unique byte stream once.
        # source_path is relative to DOCS; source_name preserves the user's filename.
        relative_path=Path('originals') / digest[:2] / f'{digest}{Path(filename).suffix[:20]}'
        target=(self.DOCS / relative_path).resolve()
        if not target.is_relative_to(self.DOCS.resolve()): raise ValueError('原文件存储路径无效。')
        self.ensure_dir(target.parent)
        if not target.exists(): target.write_bytes(data)
        con=self.db()
        old=con.execute('SELECT id FROM chunks WHERE planid=?',(planid,)).fetchall()
        if old and self.FTS_AVAILABLE:
            # Plain fts5 tables only support DELETE, not the 'delete' command.
            con.executemany('DELETE FROM chunks_fts WHERE rowid=?',[(r['id'],) for r in old])
        con.execute('DELETE FROM chunks WHERE planid=?',(planid,))
        con.execute("""INSERT INTO documents
        (planid,split,title,area_name,admin_level,parent_area_name,fyp_name,
         plantype_label,library_fileext,content_source,source_path,status,
         top_category,subcategory,subsubcategory,source_layer,text_len,source_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(planid) DO UPDATE SET
          title=excluded.title, source_path=excluded.source_path,
          text_len=excluded.text_len, status='ready', library_fileext=excluded.library_fileext,
          content_source=excluded.content_source, source_name=excluded.source_name""",
            (planid,'uploads',title,metadata['region'],None,None,None,
             metadata['kind'],suffix,suffix,relative_path.as_posix(),'ready',
             metadata['topic'],'用户上传',metadata['year'],'L1上传正文',len(text),filename))
        vectors=[]
        if self.embedding_enabled():
            try:
                vectors=self.embed_texts([f'{h}\n{t}' for _,h,_,_,t in chunks])
            except RuntimeError as exc:
                # 嵌入接口欠费/不可用时不能连累上传：照常建关键词索引，只是这批切片暂时没有向量。
                self.log.warning('上传建索引时向量生成失败，已改为只建关键词索引：%s',exc)
                vectors=[]
        for i,(_,h,o,frag,t) in enumerate(chunks):
            cur=con.execute('INSERT OR REPLACE INTO chunks(planid,filename,heading,ordinal,fragment,text,embedding,embedding_model) VALUES(?,?,?,?,?,?,?,?)',
                (planid,chunk_filename,h,o,frag,t,self.pack_vector(vectors[i]) if vectors else None,self.EMBEDDING_MODEL if vectors else None))
            try: con.execute('INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)',(cur.lastrowid,self.fts_terms(h,title,t)))
            except sqlite3.DatabaseError as exc: self.log.warning('切片 %s 写入全文索引失败：%s',frag,exc)
        con.commit(); con.close()
        # 返回落库标识，便于调用方（前端/自检）精确定位与清理本次上传产生的记录。
        return {'planid':planid,'chunks':len(chunks),'filename':filename,
                'source_path':relative_path.as_posix()}


