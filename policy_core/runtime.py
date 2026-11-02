"""Composition root: engine methods share only this instance's state.

Create a new PolicyEngine for each V2 request. The legacy HTTP server uses one
engine for the lifetime of that server. No source compilation or exec is used.
"""
import os
import threading
import logging
import sqlite3
from . import assets
from .config import ROOT, configure, load_env_file
from .errors import ModelUnavailable
from .database import DatabaseMixin
from .documents import DocumentsMixin
from .retrieval import RetrievalMixin
from .model_client import ModelClientMixin
from .answers import AnswersMixin
from .sessions import SessionsMixin
from .evolution import EvolutionMixin
from .elements import ElementsMixin
from .preview import PreviewMixin
from .security import SecurityMixin
from . import text, elements, sessions
from . import export_xml
from . import export_word
from . import export_excel
from . import export_slides


class PolicyEngine(
    DatabaseMixin, DocumentsMixin, RetrievalMixin, ModelClientMixin,
    AnswersMixin, SessionsMixin, EvolutionMixin, ElementsMixin,
    PreviewMixin, SecurityMixin,
):
    ModelUnavailable = ModelUnavailable
    normalized_text = staticmethod(text.normalized_text)
    is_existence_question = staticmethod(text.is_existence_question)
    tokens = staticmethod(text.tokens)
    _cn_number = staticmethod(text._cn_number)
    period_info = staticmethod(text.period_info)
    _clip = staticmethod(text._clip)
    _sentences = staticmethod(text._sentences)
    topic_of = staticmethod(text.topic_of)
    classify_document = staticmethod(text.classify_document)
    xml_escape = staticmethod(export_xml.xml_escape)
    _run = staticmethod(export_word._run)
    _para = staticmethod(export_word._para)
    _docx_table = staticmethod(export_word._docx_table)
    make_docx = staticmethod(export_word.make_docx)
    make_xlsx = staticmethod(export_excel.make_xlsx)
    _emu = staticmethod(export_slides._emu)
    _pptx_textbox = staticmethod(export_slides._pptx_textbox)
    _pptx_rect = staticmethod(export_slides._pptx_rect)
    _pptx_slide = staticmethod(export_slides._pptx_slide)
    make_pptx = staticmethod(export_slides.make_pptx)
    STOP_TOKENS = text.STOP_TOKENS
    CN_DIGITS = text.CN_DIGITS
    FIVE_YEAR_START = text.FIVE_YEAR_START
    TOPIC_RULES = text.TOPIC_RULES
    ELEMENT_DIMS = elements.ELEMENT_DIMS
    ELEMENT_RULES = elements.ELEMENT_RULES
    FOLLOWUP_HEAD = sessions.FOLLOWUP_HEAD
    INDEX = assets.INDEX
    TOKEN_GATE = assets.TOKEN_GATE
    _PREVIEW_SCRIPT = assets._PREVIEW_SCRIPT
    _PREVIEW_STYLE = assets._PREVIEW_STYLE

    def __init__(self, environ=None, *, load_env=False):
        if load_env:
            load_env_file(ROOT / '.env')
            load_env_file(ROOT / 'config.env')
        configure(self, os.environ if environ is None else environ)
        self.RESULTS = {}
        self.RESULTS_LOCK = threading.Lock()
        self._DEADLINE = threading.local()
        self._RATE = {}
        self._RATE_LOCK = threading.Lock()
        self._AUDIT_LOCK = threading.Lock()
        self._DB_INIT_LOCK = threading.Lock()
        self._DB_READY = False
        self.FTS_AVAILABLE = True
        self._SEMANTIC_WARN={'at':0.0,'msg':''}
        self._SCOPE_CACHE = {'count': -1}
        self.SESSION_LOCK = threading.Lock()
        self.SESSIONS = {}

    def startup_check(self):
        """Fail loudly at boot instead of serving a silently broken knowledge base."""
        self.ensure_dir(self.DOCS); self.ensure_dir(self.SOURCE_STORE); self.ensure_dir(self.KB_DIR)
        con=self.db()
        total=con.execute('SELECT count(*) FROM chunks').fetchone()[0]
        ready=con.execute("SELECT count(*) FROM documents WHERE status='ready'").fetchone()[0]
        empty=con.execute("SELECT count(*) FROM documents WHERE status='empty'").fetchone()[0]
        try: indexed=con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
        except sqlite3.DatabaseError: indexed=-1
        embedded=0
        if 'embedding' in {r[1] for r in con.execute('PRAGMA table_info(chunks)')}:
            embedded=con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0]
            matched=con.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND embedding_model=?',
                                (self.EMBEDDING_MODEL,)).fetchone()[0]
            stale=embedded-matched
            if stale:
                self.log.warning('有 %s 条切片向量由其它模型生成（当前 EMBEDDING_MODEL=%s），语义检索会忽略它们，'
                            '需要重跑：构建示例知识库.py --index-embeddings', stale, self.EMBEDDING_MODEL)
            embedded=matched
        con.close()
        self.log.info('知识库：%s', self.DB)
        self.log.info('文档 %s 篇（可检索 %s / 空正文 %s），切片 %s 条，全文索引 %s 条，向量 %s 条',
                 ready+empty, ready, empty, total, indexed, embedded)
        if indexed!=total:
            self.log.warning('全文索引未覆盖全部切片（%s/%s），关键词检索结果会不完整，请运行 rebuild_fts', indexed, total)
        if self.DATASET_ROOT.is_dir(): self.log.info('示例数据集：%s', self.DATASET_ROOT)
        else: self.log.warning('未找到示例数据集目录（原文回看可能不可用）：%s', self.DATASET_ROOT)
        if not self.environment.getenv('LLM_API_KEY','').strip(): self.log.warning('未配置 LLM_API_KEY：仅返回检索证据，不生成结论')
        if self.embedding_enabled():
            self.log.info('嵌入接口：%s（%s），模型 %s，切片向量 %s/%s', self.embedding_base(),
                     '已配置密钥' if self.embedding_key() else '回环地址免密钥', self.EMBEDDING_MODEL, embedded, total)
            if embedded<total:
                self.log.warning('向量未覆盖全部切片（%s/%s），语义检索暂不启用，请运行构建脚本 --index-embeddings', embedded, total)
        else:
            self.log.info('未配置嵌入接口：仅关键词检索。二选一：设置 EMBEDDING_API_KEY，'
                     '或把 EMBEDDING_BASE_URL 指到本机服务（如 http://127.0.0.1:11434/v1，免密钥）。')


    def main(self):
        from http.server import ThreadingHTTPServer
        from .http import make_handler
        logging.basicConfig(level=self.environment.getenv('LOG_LEVEL', 'INFO'),
                            format='%(asctime)s %(levelname)s %(message)s')
        self.enforce_access_control()
        self.startup_check()
        print(f'管策智研已启动：http://{self.HOST}:{self.PORT}')
        print('按 Ctrl+C 停止。' + ('仅绑定本机回环地址。' if self.is_loopback(self.HOST)
              else f'当前绑定 {self.HOST}，已启用令牌鉴权与限流。'))
        with ThreadingHTTPServer((self.HOST, self.PORT), make_handler(self)) as server:
            server.daemon_threads = True
            server.serve_forever()
