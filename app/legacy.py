"""Request-isolated access to policy_core; public SQLite stays read-only.

Each V2 request gets its own engine and environment snapshot. User credentials
are provided by clients, never placed in os.environ or shared module globals.
"""
from pathlib import Path
import os
import sqlite3

from policy_core import PolicyEngine
from policy_core.config import ROOT
from policy_core.errors import ModelUnavailable


def engine(public_path=None, chat=None, embedding=None):
    """Construct an isolated runtime without loading legacy .env files."""
    env = dict(os.environ)
    # Only capability checks use this marker; actual calls use the given client.
    env['LLM_API_KEY'] = 'request-client-present' if chat else ''
    runtime = PolicyEngine(environ=env)
    public = Path(public_path or ROOT / 'knowledge.sqlite3').resolve()

    def connect():
        con = sqlite3.connect(public.as_uri() + '?mode=ro', uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        return con

    def call(messages, temperature=0, max_tokens=None):
        if not chat:
            return None
        try:
            return chat.chat(messages)
        except RuntimeError as exc:
            raise ModelUnavailable('模型暂时不可用') from exc

    runtime.db = connect
    runtime.model_call = call
    runtime._DB_READY = True
    runtime.embedding_enabled = lambda: embedding is not None
    runtime.embed_texts = embedding.embed if embedding else lambda texts: []
    runtime.EMBEDDING_MODEL = 'BAAI/bge-m3'
    return runtime
