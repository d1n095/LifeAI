"""Durable resumable upload session with a bounded spool.

A session may *declare* a 5 GB+ total. That is a contract, not a measurement.
HTTP/Caddy limits stay unchanged. Finalize hashes the spool, so a crash resume
does not reread the original source and does not reset the content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.large_file_lane.stream import TARGET_FILE_BYTES, five_gb_not_supported

MANIFEST = "manifest.json"
SPOOL = "spool.bin"


class SessionIntegrityError(RuntimeError):
    """Durable session state cannot be reconciled without inventing bytes."""


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class SessionManifest:
    session_id: str
    declared_total_bytes: int
    offset: int
    parts_accepted: int
    chunk_size: int
    finalized: bool = False
    sha256: str = ""
    indexed: bool = False
    understood: bool = False

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row.update(five_gb_not_supported())
        row["declared_meets_5gb_target"] = self.declared_total_bytes >= TARGET_FILE_BYTES
        row["complete"] = self.offset == self.declared_total_bytes and self.declared_total_bytes > 0
        return row


class ResumableUploadSession:
    """Filesystem-backed session. No new Alembic table.

    Production HTTP is ``app.routers.library_resumable`` on top of this store.
    """

    def __init__(self, root: Path, *, manifest: SessionManifest) -> None:
        self.root = Path(root)
        self.manifest = manifest
        self.spool_path = self.root / SPOOL
        self.peak_part_bytes = 0

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        session_id: str,
        declared_total_bytes: int,
        chunk_size: int = 64 * 1024,
    ) -> "ResumableUploadSession":
        if declared_total_bytes <= 0:
            raise ValueError("declared_total_bytes must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        manifest = SessionManifest(
            session_id=session_id,
            declared_total_bytes=int(declared_total_bytes),
            offset=0,
            parts_accepted=0,
            chunk_size=int(chunk_size),
        )
        session = cls(root, manifest=manifest)
        with session.spool_path.open("xb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(root)
        session._persist()
        return session

    @classmethod
    def load(cls, root: Path) -> "ResumableUploadSession":
        root = Path(root)
        try:
            payload = json.loads((root / MANIFEST).read_text())
            session = cls(root, manifest=SessionManifest(**payload))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SessionIntegrityError("session manifest is unreadable") from exc
        session._validate_manifest()
        session._reconcile_spool()
        return session

    def write_part(self, data: bytes, *, expected_offset: int) -> dict[str, Any]:
        self._reconcile_spool()
        if self.manifest.finalized:
            return {"outcome": "rejected", "reason": "session already finalized", **self.manifest.public()}
        if expected_offset != self.manifest.offset:
            return {
                "outcome": "rejected",
                "reason": "expected offset does not match committed offset",
                "code": "offset_mismatch",
                **self.manifest.public(),
            }
        if not data:
            return {"outcome": "rejected", "reason": "empty part", **self.manifest.public()}
        if len(data) > self.manifest.chunk_size:
            return {"outcome": "rejected", "reason": "part exceeds chunk_size", **self.manifest.public()}
        if self.manifest.offset + len(data) > self.manifest.declared_total_bytes:
            return {"outcome": "rejected", "reason": "part would exceed declared_total_bytes", **self.manifest.public()}
        self.peak_part_bytes = max(self.peak_part_bytes, len(data))
        with self.spool_path.open("r+b") as handle:
            handle.seek(self.manifest.offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self.manifest.offset += len(data)
        self.manifest.parts_accepted += 1
        self._persist()
        return {"outcome": "accepted", "offset": self.manifest.offset, **self.manifest.public()}

    def finalize(self) -> dict[str, Any]:
        self._reconcile_spool()
        if self.manifest.offset != self.manifest.declared_total_bytes:
            return {
                "outcome": "incomplete",
                "reason": "declared total not yet received",
                **self.manifest.public(),
            }
        hasher = hashlib.sha256()
        with self.spool_path.open("rb") as handle:
            while True:
                chunk = handle.read(self.manifest.chunk_size)
                if not chunk:
                    break
                self.peak_part_bytes = max(self.peak_part_bytes, len(chunk))
                hasher.update(chunk)
        if self.spool_path.stat().st_size != self.manifest.declared_total_bytes:
            raise SessionIntegrityError("spool size changed during finalize")
        self.manifest.sha256 = hasher.hexdigest()
        self.manifest.finalized = True
        self.manifest.indexed = False
        self.manifest.understood = False
        self._persist()
        return {
            "outcome": "stored",
            "stored": True,
            "indexed": False,
            "understood": False,
            "peak_part_bytes": self.peak_part_bytes,
            **self.manifest.public(),
        }

    def _persist(self) -> None:
        tmp = self.root / f".{MANIFEST}.tmp"
        with tmp.open("w") as handle:
            json.dump(asdict(self.manifest), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.root / MANIFEST)
        _fsync_directory(self.root)

    def _validate_manifest(self) -> None:
        manifest = self.manifest
        if manifest.declared_total_bytes <= 0 or manifest.chunk_size <= 0:
            raise SessionIntegrityError("session manifest contains invalid bounds")
        if manifest.offset < 0 or manifest.offset > manifest.declared_total_bytes:
            raise SessionIntegrityError("session manifest contains an invalid offset")
        if manifest.parts_accepted < 0:
            raise SessionIntegrityError("session manifest contains an invalid part count")
        if manifest.finalized and (
            manifest.offset != manifest.declared_total_bytes or not manifest.sha256
        ):
            raise SessionIntegrityError("finalized session manifest is incomplete")

    def _reconcile_spool(self) -> None:
        """Discard only bytes that were never committed by a durable manifest.

        The spool is fsynced before the manifest.  Therefore a longer spool is an
        interrupted append and can be rolled back to the last committed offset.  A shorter
        spool means the manifest claims bytes that do not exist and must fail closed.
        """
        try:
            size = self.spool_path.stat().st_size
        except OSError as exc:
            raise SessionIntegrityError("session spool is unavailable") from exc
        if size < self.manifest.offset:
            raise SessionIntegrityError("session spool is shorter than its committed offset")
        if size == self.manifest.offset:
            return
        if self.manifest.finalized:
            raise SessionIntegrityError("finalized session spool has unexpected bytes")
        with self.spool_path.open("r+b") as handle:
            handle.truncate(self.manifest.offset)
            handle.flush()
            os.fsync(handle.fileno())
