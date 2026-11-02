"""Small, database-backed authentication primitives for the application.

This module deliberately does not implement email delivery or HTTP cookies.
Callers must obtain the raw token from a trusted session cookie/header and must
never log it. Only its SHA-256 digest is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Optional


class AuthenticationError(Exception):
    """The request has no valid authenticated principal."""


class AuthorizationError(Exception):
    """The principal cannot access the requested resource."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: Optional[str] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ValueError("session token must be a non-empty string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """Opaque bearer-session lifecycle on top of ``Database.connect()``.

    The connection is supplied by the caller so session changes can participate
    in its transaction. Methods intentionally do not commit it.
    """

    def __init__(self, database: Any, session_ttl_seconds: int = 60 * 60 * 24 * 30):
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self.database = database
        self.session_ttl_seconds = int(session_ttl_seconds)

    def create_session(self, conn: Any, user_id: str, email: Optional[str] = None):
        """Create a session; return ``(raw_token, expires_at_iso)`` once only."""
        token = secrets.token_urlsafe(32)
        now = _utc_now()
        expires = now + timedelta(seconds=self.session_ttl_seconds)
        session_id = secrets.token_urlsafe(18)
        # `email` is not stored on sessions; authenticate reads the current user
        # record, so a changed/disabled account is reflected immediately.
        del email
        conn.execute(
            "INSERT INTO sessions "
            "(session_id,user_id,token_hash,created_at,expires_at,revoked_at,last_seen_at) "
            "SELECT ?,user_id,?,?,?,NULL,? FROM users WHERE user_id=? AND status='active'",
            (session_id, token_digest(token), _iso(now), _iso(expires), _iso(now), user_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AuthenticationError("active user not found")
        return token, _iso(expires)

    def authenticate(self, conn: Any, token: str) -> Optional[Principal]:
        """Return the active principal for a valid, unexpired, unrevoked token."""
        if not token:
            return None
        now = _iso(_utc_now())
        row = conn.execute(
            "SELECT u.user_id,u.email FROM sessions s "
            "JOIN users u ON u.user_id=s.user_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? "
            "AND u.status='active'",
            (token_digest(token), now),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, token_digest(token)))
        return Principal(user_id=row["user_id"], email=row["email"])

    def revoke_session(self, conn: Any, token: str) -> bool:
        """Revoke exactly the supplied session token."""
        if not token:
            return False
        cur = conn.execute(
            "UPDATE sessions SET revoked_at=?,last_seen_at=? "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (_iso(_utc_now()), _iso(_utc_now()), token_digest(token)),
        )
        return cur.rowcount == 1


# Fixed SQL avoids interpolating a caller-controlled table or column name.
# The queries include user ownership and, for project resources, project scope.
_OWNER_QUERIES = {
    "projects": ("SELECT * FROM projects WHERE project_id=? AND user_id=?", "project_id"),
    "conversations": ("SELECT * FROM conversations WHERE conversation_id=? AND user_id=?", "conversation_id"),
    "messages": ("SELECT * FROM messages WHERE message_id=? AND user_id=? "
                 "AND EXISTS (SELECT 1 FROM projects p WHERE p.project_id=messages.project_id "
                 "AND p.user_id=messages.user_id)", "message_id"),
    "conversation_summaries": ("SELECT cs.*,c.project_id FROM conversation_summaries cs "
                 "JOIN conversations c ON c.conversation_id=cs.conversation_id "
                 "WHERE cs.conversation_id=? AND c.user_id=?", "conversation_id"),
    "project_summaries": ("SELECT * FROM project_summaries WHERE project_id=? AND user_id=?", "project_id"),
    "memories": ("SELECT * FROM memories WHERE memory_id=? AND user_id=? "
                 "AND (project_id IS NULL OR EXISTS (SELECT 1 FROM projects p "
                 "WHERE p.project_id=memories.project_id AND p.user_id=memories.user_id))", "memory_id"),
    "project_files": ("SELECT * FROM project_files WHERE file_id=? AND user_id=? "
                 "AND EXISTS (SELECT 1 FROM projects p WHERE p.project_id=project_files.project_id "
                 "AND p.user_id=project_files.user_id)", "file_id"),
    "private_documents": ("SELECT * FROM private_documents WHERE document_id=? AND user_id=?", "document_id"),
    "memory_jobs": ("SELECT * FROM memory_jobs WHERE job_id=? AND user_id=? "
                 "AND (project_id IS NULL OR EXISTS (SELECT 1 FROM projects p "
                 "WHERE p.project_id=memory_jobs.project_id AND p.user_id=memory_jobs.user_id))", "job_id"),
}


def require_owner(conn: Any, principal: Optional[Principal], table: str, resource_id: str):
    """Return an owned row or raise; unknown resources fail closed.

    Resource names must come from the fixed whitelist above, never directly
    from an HTTP parameter. Project-scoped resources are joined back to their
    owner project as a defense in depth check.
    """
    if principal is None or not principal.user_id:
        raise AuthenticationError("authentication required")
    query = _OWNER_QUERIES.get(table)
    if query is None:
        raise AuthorizationError("unsupported resource type")
    sql, _ = query
    row = conn.execute(sql, (resource_id, principal.user_id)).fetchone()
    if row is None:
        # Hide whether a resource exists but belongs to somebody else.
        raise AuthorizationError("resource not found")
    if "status" in row.keys() and row["status"] == "deleted":
        raise AuthorizationError("resource not found")
    if "project_id" in row.keys() and row["project_id"]:
        parent = conn.execute("SELECT status FROM projects WHERE project_id=? AND user_id=?", (row["project_id"], principal.user_id)).fetchone()
        if parent is None or parent["status"] == "deleted":
            raise AuthorizationError("resource not found")
    if "status" in row.keys() and row["status"] == "deleted":
        raise AuthorizationError("resource not found")
    if "project_id" in row.keys() and row["project_id"]:
        project = conn.execute("SELECT status FROM projects WHERE project_id=? AND user_id=?", (row["project_id"], principal.user_id)).fetchone()
        if project is None or project["status"] == "deleted":
            raise AuthorizationError("resource not found")
    return row
