"""Versioned application database, deliberately isolated from the public KB.

This module does not migrate, open, or otherwise modify knowledge.sqlite3.
Migrations are additive. Existing databases are backed up only when an upgrade
is required; initialization of a new database does not create a backup.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Iterator


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
APPLICATION_SCHEMA_VERSION = max((int(path.name.split("_", 1)[0]) for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")), default=0)
DEFAULT_BUSY_TIMEOUT_MS = 10_000


class MigrationError(RuntimeError):
    """An application database migration failed safely."""


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000,
                              isolation_level="DEFERRED")
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        # WAL is persistent per database and safe to request on each connection.
        con.execute("PRAGMA journal_mode = WAL")
        return con

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _migration_files(self) -> list[tuple[int, Path]]:
        found = []
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"):
            try:
                version = int(path.name.split("_", 1)[0])
            except ValueError as exc:
                raise MigrationError(f"Invalid migration filename: {path.name}") from exc
            found.append((version, path))
        found.sort()
        versions = [version for version, _ in found]
        if len(set(versions)) != len(versions):
            raise MigrationError("Duplicate migration versions")
        return found

    def _backup_existing(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.path.with_name(f"{self.path.name}.pre-migration-{stamp}.bak")
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            result = destination.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise MigrationError(f"Backup quick_check failed: {result}")
        except BaseException:
            destination.close()
            source.close()
            target.unlink(missing_ok=True)
            raise
        destination.close()
        source.close()
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    @staticmethod
    def _validate(con: sqlite3.Connection) -> None:
        result = con.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise MigrationError(f"Database quick_check failed: {result}")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = tuple(violations[0])
            raise MigrationError(f"Foreign key violations found: {first}")

    def initialize(self) -> int:
        """Migrate in one live-file transaction; never replace an active inode.

        Run before accepting requests. DDL/data changes roll back on any error;
        a verified pre-migration snapshot supports an offline manual restore.
        """
        files = self._migration_files()
        if [v for v, _ in files] != list(range(1, len(files) + 1)):
            raise MigrationError("Migration versions must be contiguous starting at 1")
        target_version = files[-1][0] if files else 0
        existed = self.path.is_file() and self.path.stat().st_size > 0
        current = self._read_version(self.path) if existed else 0
        if current > target_version:
            raise MigrationError("Database schema is newer than supported migrations")
        backup = self._backup_existing() if existed and current < target_version else None
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT version,name,checksum FROM schema_migrations")}
            if sorted(known) != list(range(1, len(known) + 1)):
                raise MigrationError("Database migration history contains a gap")
            for version, path in files:
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in known:
                    if known[version] != (path.name, checksum):
                        raise MigrationError(f"Applied migration {version} was modified")
                    continue
                for statement in _split_sql(sql):
                    con.execute(statement)
                con.execute("INSERT INTO schema_migrations VALUES(?,?,?,?)", (version,path.name,checksum,datetime.now(timezone.utc).isoformat(timespec="seconds")))
            self._validate(con)
            con.commit()
            return target_version
        except BaseException as exc:
            con.rollback()
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"Migration failed and rolled back. Snapshot: {backup or 'none (new database)'}") from exc
        finally:
            con.close()

    @staticmethod
    def _read_version(path: Path) -> int:
        con = sqlite3.connect(path)
        try:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            if not exists:
                return 0
            return int(con.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
        finally:
            con.close()


def _split_sql(script: str) -> list[str]:
    """Use SQLite's parser to recognize complete statements, including triggers."""
    statements = []
    buffer = ""
    for char in script:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        if sqlite3.complete_statement(buffer + ";"):
            statements.append(buffer)
        else:
            raise MigrationError("Incomplete SQL migration")
    return statements
