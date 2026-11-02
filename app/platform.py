"""Application services for accounts, projects, conversations and memories.

No credential values are persisted here. HTTP adapters must derive the
Principal from the server-side session cookie before calling these methods.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
from email.message import EmailMessage
import hashlib
import hmac
import json
import os
import secrets
import smtplib
import ssl
import uuid

from .auth import AuthService, Principal, AuthenticationError, AuthorizationError, require_owner


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def opaque_id():
    return str(uuid.uuid4())


class EmailDeliveryError(RuntimeError):
    pass


class SMTPMailer:
    """SMTP adapter configured only from server environment variables."""
    def send_code(self, email: str, code: str, purpose: str) -> None:
        host = os.getenv("SMTP_HOST", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip()
        if not host or not sender:
            raise EmailDeliveryError("Email delivery is not configured")
        message = EmailMessage()
        message["Subject"] = "登录验证码" if purpose == "login" else "注册验证码"
        message["From"] = sender
        message["To"] = email
        message.set_content(f"您的验证码是 {code}，10 分钟内有效。若非本人操作请忽略。")
        port = int(os.getenv("SMTP_PORT", "465"))
        user, password = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
                    if user: smtp.login(user, password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=15) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if user: smtp.login(user, password)
                    smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError("Email delivery failed") from exc


class Platform:
    def __init__(self, database, *, mailer=None, code_pepper: str | None = None):
        self.db = database
        self.mailer = mailer or SMTPMailer()
        self.code_pepper = code_pepper or os.getenv("AUTH_CODE_PEPPER", "")
        self.auth = AuthService(database)

    @staticmethod
    def _password_digest(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 310_000).hex()

    @staticmethod
    def _validate_credentials(email: str, password: str) -> tuple[str, str]:
        email = email.strip().casefold()
        import re
        if (len(email) > 254 or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", email)
                or not isinstance(password, str) or not 10 <= len(password) <= 256):
            raise ValueError('邮箱格式无效，密码长度须为 10 至 256 个字符')
        return email, password

    def register_password(self, email: str, password: str) -> tuple[str, str]:
        email, password = self._validate_credentials(email, password)
        salt = secrets.token_bytes(16)
        digest = self._password_digest(password, salt)
        user_id, now = opaque_id(), now_iso()
        with self.db.transaction(immediate=True) as con:
            if con.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                raise ValueError('该邮箱已注册，请直接登录')
            con.execute('INSERT INTO users(user_id,email,email_verified_at,password_hash,password_salt,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (user_id,email,now,digest,salt.hex(),now,now))
            return self.auth.create_session(con, user_id)

    def login_password(self, email: str, password: str) -> tuple[str, str]:
        email = email.strip().casefold() if isinstance(email, str) else ''
        if len(email) > 254 or not isinstance(password, str) or len(password) > 256:
            raise AuthenticationError('邮箱或密码错误')
        with self.db.transaction(immediate=True) as con:
            user = con.execute("SELECT user_id,password_hash,password_salt FROM users WHERE email=? AND status='active'", (email,)).fetchone()
            # Do the same expensive derivation for unknown accounts to reduce timing-based enumeration.
            salt = bytes.fromhex(user['password_salt']) if user and user['password_salt'] else bytes(16)
            candidate = self._password_digest(password or 'invalid-password', salt)
            if (not user or not user['password_hash'] or
                    not hmac.compare_digest(user['password_hash'], candidate)):
                raise AuthenticationError('邮箱或密码错误；旧验证码账户请联系管理员重置密码')
            return self.auth.create_session(con, user['user_id'])

    def _code_hash(self, email: str, purpose: str, code: str) -> str:
        if not self.code_pepper:
            raise RuntimeError("AUTH_CODE_PEPPER must be configured")
        msg = f"{email.casefold()}:{purpose}:{code}".encode()
        return hmac.new(self.code_pepper.encode(), msg, hashlib.sha256).hexdigest()

    def request_code(self, email: str, purpose: str) -> None:
        email = email.strip().casefold()
        import re
        if (len(email) > 254 or "\r" in email or "\n" in email
                or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", email)
                or purpose not in ("register", "login")):
            raise ValueError("Invalid email or purpose")
        with self.db.transaction(immediate=True) as con:
            recent = con.execute("SELECT count(*) FROM email_verification_codes WHERE email=? AND created_at>?",
                (email, (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(timespec="seconds"))).fetchone()[0]
            if recent >= 3:
                raise ValueError("Too many verification requests; try again later")
            if purpose == "register" and con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise ValueError("Email is already registered")
            if purpose == "login" and not con.execute("SELECT 1 FROM users WHERE email=? AND status='active'", (email,)).fetchone():
                raise ValueError("Account not found")
            con.execute("UPDATE email_verification_codes SET consumed_at=? WHERE email=? AND purpose=? AND consumed_at IS NULL",(now_iso(),email,purpose))
            code = f"{secrets.randbelow(1_000_000):06d}"
            created = now_iso()
            con.execute("INSERT INTO email_verification_codes(code_id,email,purpose,code_hash,expires_at,attempts,max_attempts,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (opaque_id(), email, purpose, self._code_hash(email,purpose,code),
                 (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(timespec="seconds"), 0, 5, created))
        # Send only after commit; failed delivery leaves a short-lived unusable code.
        self.mailer.send_code(email, code, purpose)

    def verify_code(self, email: str, purpose: str, code: str) -> tuple[str, str]:
        email = email.strip().casefold()
        now = now_iso()
        candidate = self._code_hash(email, purpose, code)
        invalid = False
        with self.db.transaction(immediate=True) as con:
            row = con.execute("SELECT * FROM email_verification_codes WHERE email=? AND purpose=? AND consumed_at IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 1", (email,purpose)).fetchone()
            if row is None or row["expires_at"] <= now or row["attempts"] >= row["max_attempts"]:
                raise AuthenticationError("Invalid or expired verification code")
            if not hmac.compare_digest(row["code_hash"], candidate):
                con.execute("UPDATE email_verification_codes SET attempts=attempts+1 WHERE code_id=?",(row["code_id"],))
                invalid = True
            if invalid:
                pass
            else:
                return self._complete_verification(con, row, email, purpose, now)
        if invalid:
            raise AuthenticationError("Invalid or expired verification code")

    def _complete_verification(self, con, row, email, purpose, now):
        con.execute("UPDATE email_verification_codes SET consumed_at=? WHERE code_id=? AND consumed_at IS NULL", (now,row["code_id"]))
        if purpose == "register":
            user_id, ts = opaque_id(), now
            con.execute("INSERT INTO users(user_id,email,email_verified_at,created_at,updated_at) VALUES(?,?,?,?,?)", (user_id,email,ts,ts,ts))
        else:
            user = con.execute("SELECT user_id FROM users WHERE email=? AND status='active'",(email,)).fetchone()
            if not user: raise AuthenticationError("Account not found")
            user_id = user["user_id"]
        token, expiry = self.auth.create_session(con,user_id)
        return token, expiry

    def authenticate(self, token: str) -> Principal | None:
        with self.db.transaction() as con:
            return self.auth.authenticate(con, token)

    def logout(self, token: str) -> bool:
        with self.db.transaction(immediate=True) as con:
            return self.auth.revoke_session(con,token)

    def create_project(self, principal: Principal, name: str, description: str = "") -> dict:
        name = name.strip()
        if not name or len(name) > 160: raise ValueError("Project name must be 1–160 characters")
        pid, ts = opaque_id(), now_iso()
        with self.db.transaction(immediate=True) as con:
            con.execute("INSERT INTO projects(project_id,user_id,name,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",(pid,principal.user_id,name,description[:10000],ts,ts))
            con.execute("INSERT INTO project_summaries(project_id,user_id,summary,created_at,updated_at) VALUES(?,?,?, ?,?)",(pid,principal.user_id,"",ts,ts))
        return {"project_id":pid,"user_id":principal.user_id,"name":name,"description":description,"status":"active","created_at":ts,"updated_at":ts}

    def list_projects(self, principal: Principal) -> list[dict]:
        with closing(self.db.connect()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM projects WHERE user_id=? AND status!='deleted' ORDER BY updated_at DESC",(principal.user_id,))]

    def update_project(self, principal: Principal, project_id: str, **fields) -> dict:
        allowed={k:v for k,v in fields.items() if k in ("name","description","status")}
        if not allowed: raise ValueError("No editable project fields")
        if "name" in allowed and (not isinstance(allowed["name"],str) or not allowed["name"].strip() or len(allowed["name"])>160): raise ValueError("Invalid project name")
        if "description" in allowed:
            if not isinstance(allowed["description"],str): raise ValueError("Invalid project description")
            allowed["description"]=allowed["description"][:10000]
        if "status" in allowed and allowed["status"] not in ("active","archived"): raise ValueError("Invalid project status")
        if "name" in allowed: allowed["name"]=allowed["name"].strip()
        allowed["updated_at"] = now_iso()
        with self.db.transaction(immediate=True) as con:
            self._project(con,principal,project_id)
            assignments=", ".join(f"{k}=?" for k in allowed)
            con.execute(f"UPDATE projects SET {assignments} WHERE project_id=? AND user_id=?",(*allowed.values(),project_id,principal.user_id))
            return dict(require_owner(con,principal,"projects",project_id))

    def delete_project(self, principal: Principal, project_id: str) -> None:
        # Soft deletion retains auditability; cascade deletion is an explicit later policy.
        with self.db.transaction(immediate=True) as con:
            self._project(con,principal,project_id)
            con.execute("UPDATE projects SET status='deleted',updated_at=? WHERE project_id=? AND user_id=?",(now_iso(),project_id,principal.user_id))
            con.execute("UPDATE conversations SET status='deleted' WHERE project_id=? AND user_id=?",(project_id,principal.user_id))
            con.execute("UPDATE memories SET status='deleted' WHERE project_id=? AND user_id=?",(project_id,principal.user_id))
            con.execute("UPDATE project_files SET status='deleted' WHERE project_id=? AND user_id=?",(project_id,principal.user_id))
            con.execute("UPDATE memory_jobs SET status='cancelled' WHERE project_id=? AND user_id=? AND status IN ('pending','running')",(project_id,principal.user_id))

    def create_conversation(self, principal: Principal, project_id: str, title: str = "新对话") -> str:
        cid, ts=opaque_id(),now_iso()
        with self.db.transaction(immediate=True) as con:
            self._project(con,principal,project_id)
            con.execute("INSERT INTO conversations(conversation_id,user_id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",(cid,principal.user_id,project_id,title[:160],ts,ts))
        return cid

    def add_message(self, principal: Principal, conversation_id: str, role: str, content: str) -> str:
        if role not in ("user","assistant") or not content or len(content)>200_000: raise ValueError("Invalid message")
        mid,ts=opaque_id(),now_iso()
        with self.db.transaction(immediate=True) as con:
            row=con.execute("SELECT * FROM conversations WHERE conversation_id=? AND user_id=? AND status='active'",(conversation_id,principal.user_id)).fetchone()
            if row is None: raise AuthorizationError("resource not found")
            self._project(con,principal,row["project_id"])
            con.execute("INSERT INTO messages(message_id,user_id,project_id,conversation_id,role,content,created_at) VALUES(?,?,?,?,?,?,?)",(mid,principal.user_id,row["project_id"],conversation_id,role,content,ts))
            con.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?",(ts,conversation_id))
        return mid

    def get_conversation(self, principal: Principal, conversation_id: str) -> dict:
        with closing(self.db.connect()) as con:
            conv=self._conversation(con,principal,conversation_id)
            messages=[dict(r) for r in con.execute("SELECT message_id,role,content,created_at FROM messages WHERE conversation_id=? AND user_id=? ORDER BY rowid",(conversation_id,principal.user_id))]
            summary=con.execute("SELECT summary FROM conversation_summaries WHERE conversation_id=?",(conversation_id,)).fetchone()
            return {"conversation":dict(conv),"messages":messages,"summary":summary[0] if summary else ""}

    def save_memory(self, principal: Principal, content: str, *, project_id: str|None=None, category="fact", importance=.5, confidence=.8, source_conversation_id=None) -> str:
        if not content.strip() or len(content)>8000: raise ValueError("Invalid memory content")
        # Defense in depth: reject likely credentials before persistence.
        if re_secret(content): raise ValueError("Secrets and credentials cannot be saved as memory")
        if not (0<=float(importance)<=1 and 0<=float(confidence)<=1): raise ValueError("Invalid memory score")
        mid,ts=opaque_id(),now_iso()
        with self.db.transaction(immediate=True) as con:
            if project_id: self._project(con,principal,project_id)
            if source_conversation_id:
                source=self._conversation(con,principal,source_conversation_id)
                if project_id and source["project_id"] != project_id: raise AuthorizationError("resource not found")
            similar=con.execute("SELECT memory_id FROM memories WHERE user_id=? AND project_id IS ? AND category=? AND status='active' AND content=?",(principal.user_id,project_id,category,content.strip())).fetchone()
            if similar: return similar[0]
            con.execute("INSERT INTO memories(memory_id,user_id,project_id,category,content,importance,confidence,source_conversation_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(mid,principal.user_id,project_id,category,content.strip(),float(importance),float(confidence),source_conversation_id,ts,ts))
        return mid

    def _project(self, con, principal, project_id):
        row=require_owner(con,principal,"projects",project_id)
        if row["status"] == "deleted": raise AuthorizationError("resource not found")
        return row

    def _conversation(self, con, principal, conversation_id):
        row=require_owner(con,principal,"conversations",conversation_id)
        if row["status"] == "deleted": raise AuthorizationError("resource not found")
        self._project(con,principal,row["project_id"])
        return row

    def retrieve_context(self, principal, project_id, query, *, conversation_id=None, recent_messages=12, memory_limit=8):
        from .memory import MemoryService
        return MemoryService(self).context(principal,project_id,conversation_id,query)


def re_secret(value: str) -> bool:
    """Conservative credential-pattern guard, including Chinese labels.

    This cannot recognize every unlabeled secret; callers also avoid persisting
    request model configurations and redact known runtime credentials.
    """
    import re
    return bool(re.search(
        r"(?i)(?:sk-[a-z0-9_-]{12,}|\bBearer\s+[a-z0-9._~+/-]{6,}=*|"
        r"(?:api[ _-]?key|access[ _-]?token|token|password|secret|authorization|"
        r"密钥|秘钥|密码|口令|令牌|验证码|授权码)\s*(?:[:=：]|是|为)\s*\S+|"
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
        r"\b(?:AKIA|LTAI)[A-Z0-9]{12,}|\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)", str(value)))
