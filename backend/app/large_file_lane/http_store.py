"""Founder-owned resumable HTTP store on top of #313 sessions.

Each HTTP part stays at 64 KiB so Caddy's 30 MB request body and
``library.MAX_UPLOAD_BYTES`` do not need to change. A session may declare the
5 GB target. That is a reservation contract, not measured 5×5 GB support.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID

from app.large_file_lane.session import MANIFEST, ResumableUploadSession, SessionIntegrityError
from app.large_file_lane.stream import DEFAULT_CHUNK, TARGET_FILE_BYTES, five_gb_not_supported

HTTP_PART_MAX_BYTES = DEFAULT_CHUNK
HTTP_DECLARED_MAX_BYTES = TARGET_FILE_BYTES
OWNER_QUOTA_BYTES = 5 * TARGET_FILE_BYTES
DISK_HEADROOM_BYTES = 512 * 1024 * 1024
SESSION_SUBDIR = "resumable_uploads"
OWNER_META = "owner.json"
LOCK_NAME = "session.lock"


class ResumableHttpError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def sanitize_filename(name: str | None) -> str | None:
    if name is None:
        return None
    text = name.strip()
    if not text:
        raise ResumableHttpError("invalid_filename", "filename must not be empty")
    if len(text) > 255:
        raise ResumableHttpError("invalid_filename", "filename too long")
    if any(sep in text for sep in ("/", "\\", "\0")) or text in {".", ".."} or ".." in text:
        raise ResumableHttpError("invalid_filename", "filename must be a basename")
    return text


def parse_session_id(raw: str) -> UUID:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ResumableHttpError("invalid_session_id", "session_id must be a UUID") from exc


@contextmanager
def _session_lock(root: Path, *, create: bool = False) -> Iterator[None]:
    if create:
        root.mkdir(parents=True, exist_ok=True)
    try:
        handle = (root / LOCK_NAME).open("a+")
    except FileNotFoundError as exc:
        raise ResumableHttpError("not_found", "session not found", http_status=404) from exc
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ResumableHttpStore:
    """Owner-scoped filesystem store. No Alembic table. No 5 GB claim."""

    def __init__(
        self,
        root: Path,
        *,
        owner_quota_bytes: int = OWNER_QUOTA_BYTES,
        disk_headroom_bytes: int = DISK_HEADROOM_BYTES,
        disk_free_fn: Callable[[Path], int] | None = None,
        declared_max_bytes: int = HTTP_DECLARED_MAX_BYTES,
        part_max_bytes: int = HTTP_PART_MAX_BYTES,
    ) -> None:
        self.root = Path(root)
        self.owner_quota_bytes = int(owner_quota_bytes)
        self.disk_headroom_bytes = int(disk_headroom_bytes)
        self.disk_free_fn = disk_free_fn or (lambda path: shutil.disk_usage(path).free)
        self.declared_max_bytes = int(declared_max_bytes)
        self.part_max_bytes = int(part_max_bytes)

    def owner_root(self, owner_id: UUID) -> Path:
        base = self.root.resolve()
        path = (self.root / str(owner_id)).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ResumableHttpError("invalid_owner", "owner path escapes store root") from exc
        return path

    def session_root(self, owner_id: UUID, session_id: UUID) -> Path:
        owner = self.owner_root(owner_id)
        path = (owner / str(session_id)).resolve()
        try:
            path.relative_to(owner)
        except ValueError as exc:
            raise ResumableHttpError("invalid_session_id", "session path escapes owner root") from exc
        return path

    def reserved_bytes(self, owner_id: UUID) -> int:
        root = self.owner_root(owner_id)
        if not root.exists():
            return 0
        total = 0
        for child in root.iterdir():
            manifest = child / MANIFEST
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text())
                total += int(payload.get("declared_total_bytes") or 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ResumableHttpError(
                    "session_integrity_error",
                    "owner reservation state is unreadable",
                    http_status=409,
                ) from exc
        return total

    def create(
        self,
        owner_id: UUID,
        *,
        declared_total_bytes: int,
        declared_filename: str | None = None,
    ) -> dict[str, Any]:
        if declared_total_bytes <= 0:
            raise ResumableHttpError("invalid_declared_total", "declared_total_bytes must be positive")
        if declared_total_bytes > self.declared_max_bytes:
            raise ResumableHttpError(
                "declared_exceeds_http_cap",
                "HTTP declaration cap is the 5 GB target; 5 GB is not certified",
                http_status=413,
            )
        filename = sanitize_filename(declared_filename)
        reserved = self.reserved_bytes(owner_id)
        if reserved + declared_total_bytes > self.owner_quota_bytes:
            raise ResumableHttpError("owner_quota_exceeded", "owner resumable quota exceeded", http_status=413)
        self.root.mkdir(parents=True, exist_ok=True)
        free = int(self.disk_free_fn(self.root))
        if free < declared_total_bytes + self.disk_headroom_bytes:
            raise ResumableHttpError(
                "disk_headroom_insufficient",
                "not enough free disk with required headroom",
                http_status=507,
            )
        session_id = uuid.uuid4()
        root = self.session_root(owner_id, session_id)
        with _session_lock(root, create=True):
            session = ResumableUploadSession.create(
                root,
                session_id=str(session_id),
                declared_total_bytes=declared_total_bytes,
                chunk_size=self.part_max_bytes,
            )
            (root / OWNER_META).write_text(
                json.dumps(
                    {"owner_id": str(owner_id), "declared_filename": filename},
                    indent=2,
                    sort_keys=True,
                )
            )
            return self._public(session, owner_id)

    def get(self, owner_id: UUID, session_id: UUID) -> dict[str, Any]:
        root = self.session_root(owner_id, session_id)
        with _session_lock(root):
            return self._public(self._load(owner_id, session_id), owner_id)

    def list_sessions(self, owner_id: UUID) -> list[dict[str, Any]]:
        root = self.owner_root(owner_id)
        if not root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for child in sorted(root.iterdir()):
            if not (child / MANIFEST).is_file():
                continue
            try:
                session_id = parse_session_id(child.name)
            except ResumableHttpError:
                continue
            with _session_lock(child):
                rows.append(self._public(self._load(owner_id, session_id), owner_id))
        return rows

    def write_part(
        self,
        owner_id: UUID,
        session_id: UUID,
        data: bytes,
        *,
        expected_offset: int,
    ) -> dict[str, Any]:
        if len(data) > self.part_max_bytes:
            raise ResumableHttpError("part_too_large", "part exceeds HTTP 64 KiB bound", http_status=413)
        root = self.session_root(owner_id, session_id)
        with _session_lock(root):
            session = self._load(owner_id, session_id)
            result = session.write_part(data, expected_offset=expected_offset)
            if result["outcome"] == "rejected":
                raise ResumableHttpError(
                    str(result.get("code") or "part_rejected"),
                    str(result.get("reason") or "part rejected"),
                    http_status=409,
                )
            return {**self._public(session, owner_id), **result}

    def finalize(self, owner_id: UUID, session_id: UUID) -> dict[str, Any]:
        root = self.session_root(owner_id, session_id)
        with _session_lock(root):
            session = self._load(owner_id, session_id)
            if session.manifest.finalized:
                return {**self._public(session, owner_id), "outcome": "stored", "already_finalized": True}
            result = session.finalize()
            if result["outcome"] == "incomplete":
                raise ResumableHttpError(
                    "incomplete",
                    str(result.get("reason") or "declared total not yet received"),
                    http_status=409,
                )
            return {**self._public(session, owner_id), **result}

    def abort(self, owner_id: UUID, session_id: UUID) -> dict[str, Any]:
        root = self.session_root(owner_id, session_id)
        if not root.exists():
            raise ResumableHttpError("not_found", "session not found", http_status=404)
        with _session_lock(root):
            if not (root / MANIFEST).is_file():
                raise ResumableHttpError("not_found", "session not found", http_status=404)
            shutil.rmtree(root)
        return {"outcome": "aborted", "session_id": str(session_id), **five_gb_not_supported()}

    def _load(self, owner_id: UUID, session_id: UUID) -> ResumableUploadSession:
        root = self.session_root(owner_id, session_id)
        if not (root / MANIFEST).is_file():
            raise ResumableHttpError("not_found", "session not found", http_status=404)
        try:
            session = ResumableUploadSession.load(root)
        except SessionIntegrityError as exc:
            raise ResumableHttpError(
                "session_integrity_error",
                "session state is inconsistent and cannot be resumed safely",
                http_status=409,
            ) from exc
        if session.manifest.session_id != str(session_id):
            raise ResumableHttpError(
                "session_integrity_error",
                "session manifest identity does not match its durable path",
                http_status=409,
            )
        return session

    def _filename(self, session: ResumableUploadSession) -> str | None:
        meta = session.root / OWNER_META
        if not meta.is_file():
            return None
        try:
            payload = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        name = payload.get("declared_filename")
        return str(name) if name else None

    def _public(self, session: ResumableUploadSession, owner_id: UUID) -> dict[str, Any]:
        row = session.manifest.public()
        row.update(
            {
                "owner_id": str(owner_id),
                "declared_filename": self._filename(session),
                "http_part_max_bytes": self.part_max_bytes,
                "http_declared_max_bytes": self.declared_max_bytes,
                "owner_quota_bytes": self.owner_quota_bytes,
                "caddy_request_body_unchanged": True,
                **five_gb_not_supported(),
            }
        )
        return row
