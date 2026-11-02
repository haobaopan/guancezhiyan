"""Persistent, owner-scoped storage for private knowledge and project files."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid


class StorageError(Exception):
    """A file could not be safely stored or resolved."""


@dataclass(frozen=True)
class StoredFile:
    storage_path: str
    source_name: str
    sha256: str
    size_bytes: int


def safe_filename(name: str) -> str:
    """Validate a display filename and reject all path syntax."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise StorageError("invalid filename")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise StorageError("filename must not contain a path")
    if Path(name).name != name or name.startswith("~"):
        raise StorageError("invalid filename")
    if len(name.encode("utf-8")) > 240:
        raise StorageError("filename is too long")
    # Prevent control characters from reaching logs, headers, or UI.
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise StorageError("filename contains control characters")
    return name


def _opaque_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StorageError(f"{field} must be an opaque UUID") from exc


class Storage:
    """Write originals under isolated user/project directories.

    Paths persisted in the application database are relative paths. Never use a
    client-supplied storage path to choose a destination; ``resolve`` checks
    both the storage root and the expected owner/scope directory.
    """

    def __init__(self, root: os.PathLike | str, max_file_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root).expanduser().resolve()
        self.max_file_bytes = int(max_file_bytes)
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def _scope_dir(self, user_id: str, project_id: str | None, kind: str) -> Path:
        uid = _opaque_uuid(user_id, "user_id")
        if kind == "knowledge":
            if project_id is not None:
                raise StorageError("knowledge files cannot have a project scope")
            base = self.root / "user_knowledge" / uid / "originals"
        elif kind == "project":
            if project_id is None:
                raise StorageError("project files require project_id")
            pid = _opaque_uuid(project_id, "project_id")
            base = self.root / "project_files" / uid / pid / "originals"
        else:
            raise StorageError("unsupported storage scope")
        resolved = base.resolve()
        if not resolved.is_relative_to(self.root):
            raise StorageError("storage path escaped root")
        return resolved

    @staticmethod
    def _extension(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        # Do not allow an untrusted extension to become path syntax.
        return ext if re.fullmatch(r"\.[a-z0-9]{1,12}", ext) else ""

    def save_user_file(self, user_id: str, data: bytes, original_name: str) -> StoredFile:
        return self._save(user_id, None, "knowledge", data, original_name)

    def save_project_file(self, user_id: str, project_id: str, data: bytes, original_name: str) -> StoredFile:
        return self._save(user_id, project_id, "project", data, original_name)

    def _save(self, user_id: str, project_id: str | None, kind: str,
              data: bytes, original_name: str) -> StoredFile:
        name = safe_filename(original_name)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise StorageError("file content must be bytes")
        payload = bytes(data)
        if len(payload) > self.max_file_bytes:
            raise StorageError("file exceeds configured size limit")
        scope = self._scope_dir(user_id, project_id, kind)
        scope.mkdir(parents=True, exist_ok=True, mode=0o700)
        # UUID filename avoids collisions and does not disclose user-supplied names.
        leaf = uuid.uuid4().hex + self._extension(name)
        destination = scope / leaf
        if not destination.resolve().is_relative_to(scope):
            raise StorageError("invalid destination")
        fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=scope)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            # Persist directory entry before returning a path to the caller.
            dir_fd = os.open(scope, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        rel = destination.relative_to(self.root).as_posix()
        return StoredFile(rel, name, hashlib.sha256(payload).hexdigest(), len(payload))

    def resolve_user_file(self, user_id: str, storage_path: str) -> Path:
        return self._resolve(user_id, None, "knowledge", storage_path)

    def resolve_project_file(self, user_id: str, project_id: str, storage_path: str) -> Path:
        return self._resolve(user_id, project_id, "project", storage_path)

    def _resolve(self, user_id: str, project_id: str | None, kind: str, storage_path: str) -> Path:
        if not isinstance(storage_path, str) or "\\" in storage_path or "\x00" in storage_path:
            raise StorageError("invalid stored path")
        rel = Path(storage_path)
        if rel.is_absolute() or any(part in {".", ".."} for part in rel.parts):
            raise StorageError("invalid stored path")
        base = self._scope_dir(user_id, project_id, kind)
        candidate = (self.root / rel).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise StorageError("file not found in this owner scope")
        return candidate

    def delete_user_file(self, user_id: str, storage_path: str) -> bool:
        return self._delete(self.resolve_user_file(user_id, storage_path))

    def delete_project_file(self, user_id: str, project_id: str, storage_path: str) -> bool:
        return self._delete(self.resolve_project_file(user_id, project_id, storage_path))

    @staticmethod
    def _delete(path: Path) -> bool:
        path.unlink()
        return True
