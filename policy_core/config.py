"""Project paths and configuration; importing this module does not load .env."""
from pathlib import Path
import logging
import os

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path):
    """Load KEY=VALUE pairs from an optional .env file.

    已经存在的环境变量优先，但**空值不算已设置**：很多 shell 模板或 CI 会先
    `export LLM_API_KEY=`（空串），若按「key in os.environ」判断，就会把 .env
    里的真实密钥挡掉，首次配置被误判成「未配置密钥」。
    """
    if not path.is_file(): return
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        if line.startswith('export '): line = line[7:].lstrip()
        key, _, value = line.partition('=')
        key = key.strip(); value = value.strip().strip('"').strip("'")
        if not key or not value: continue
        if (os.environ.get(key) or '').strip(): continue
        os.environ[key] = value


def first_dir(*candidates):
    for c in candidates:
        if not c: continue
        p = Path(c).expanduser()
        if p.is_dir(): return p.resolve()
    return None


class Environment:
    """A private snapshot; V2 request credentials never mutate os.environ."""

    def __init__(self, values):
        self.environ = dict(values)

    def getenv(self, key, default=None):
        return self.environ.get(key, default)


def configure(self, environ):
    self.ROOT = ROOT
    self.environment = Environment(environ)
    self.log = logging.getLogger('policy')
    self.DOCS = Path(self.environment.getenv('POLICY_DOCS_DIR') or (self.ROOT / 'documents')).expanduser().resolve()
    self.SOURCE_STORE = self.DOCS / 'originals'
    self._nested_kb = self.ROOT / '知识库_示例数据集_v1'
    self.KB_DIR = Path(self.environment.getenv('POLICY_KB_DIR') or (
        self.ROOT if (self.ROOT / 'knowledge.sqlite3').is_file()
        else self._nested_kb if (self._nested_kb / 'knowledge.sqlite3').is_file()
        else self.ROOT)).expanduser().resolve()
    self.DB = self.KB_DIR / 'knowledge.sqlite3'
    self.DATASET_ROOT = first_dir(
        self.environment.getenv('POLICY_DATASET_DIR'),
        self.ROOT / '管策智研_示例数据集',
        self.ROOT.parent / '管策智研_示例数据集',
        self.ROOT.parent / '管策智研检验所用数据' / '管策智研_示例数据集',
        Path.home() / 'Desktop' / '管策智研检验所用数据' / '管策智研_示例数据集',
        Path.home() / 'Desktop' / '管策智研_示例数据集',
    ) or (self.ROOT / '管策智研_示例数据集')
    self.HOST = self.environment.getenv('POLICY_HOST', '127.0.0.1')
    self.PORT = int(self.environment.getenv('POLICY_PORT', '8765'))
    self.MAX_UPLOAD = 20 * 1024 * 1024
    self.MAX_UNZIPPED = 200 * 1024 * 1024
    self.MAX_SAVED_RESULTS = 500
    self.EMBEDDING_MODEL = self.environment.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
    self.EMBEDDING_BATCH_SIZE = max(1, min(128, int(self.environment.getenv('EMBEDDING_BATCH_SIZE', '64'))))
    self.ANSWER_DEADLINE_SECONDS = float(self.environment.getenv('ANSWER_DEADLINE_SECONDS', '150'))
    self.VERIFY_CONCURRENCY = max(1, min(16, int(self.environment.getenv('VERIFY_CONCURRENCY', '6'))))
    self.ACCESS_TOKEN = self.environment.getenv('ACCESS_TOKEN', '').strip()
    self.RATE_LIMIT_PER_MIN = max(0, int(self.environment.getenv('RATE_LIMIT_PER_MIN', '120')))
    self.AUDIT_LOG = Path(self.environment.getenv('AUDIT_LOG') or (self.ROOT / 'audit.jsonl'))
    self.AUDIT_MAX_BYTES = 20 * 1024 * 1024
    self.CHUNK_MIN = max(200, int(self.environment.getenv('CHUNK_MIN_CHARS', '450')))
    self.CHUNK_MAX = max(self.CHUNK_MIN + 100, int(self.environment.getenv('CHUNK_MAX_CHARS', '800')))
    self.CHUNK_OVERLAP = max(0, min(200, int(self.environment.getenv('CHUNK_OVERLAP_CHARS', '60'))))
    self._NO_PROXY_OPENER = __import__('urllib.request', fromlist=['x']).build_opener(
        __import__('urllib.request', fromlist=['x']).ProxyHandler({}))
    self.MAX_SESSIONS = 300
