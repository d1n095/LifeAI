"""Durable resumable upload session with a bounded spool.

A session may *declare* a 5 GB+ total. That is a contract, not a measurement.
HTTP/Caddy limits stay unchanged. Finalize hashes the spool, so a crash resume
does not reread the original source and does not reset the content hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.large_file_lane.stream import TARGET_FILE_BYTES, five_gb_not_supported

MANIFEST = "manifest.json"
SPOOL = "spool.bin"


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
        session._persist()
        session.spool_path.touch(exist_ok=True)
        return session

    @classmethod
    def load(cls, root: Path) -> "ResumableUploadSession":
        payload = json.loads((Path(root) / MANIFEST).read_text())
        return cls(root, manifest=SessionManifest(**payload))

    def write_part(self, data: bytes) -> dict[str, Any]:
        if self.manifest.finalized:
            return {"outcome": "rejected", "reason": "session already finalized", **self.manifest.public()}
        if not data:
            return {"outcome": "rejected", "reason": "empty part", **self.manifest.public()}
        if len(data) > self.manifest.chunk_size:
            return {"outcome": "rejected", "reason": "part exceeds chunk_size", **self.manifest.public()}
        if self.manifest.offset + len(data) > self.manifest.declared_total_bytes:
            return {"outcome": "rejected", "reason": "part would exceed declared_total_bytes", **self.manifest.public()}
        self.peak_part_bytes = max(self.peak_part_bytes, len(data))
        with self.spool_path.open("ab") as handle:
            handle.write(data)
            handle.flush()
        self.manifest.offset += len(data)
        self.manifest.parts_accepted += 1
        self._persist()
        return {"outcome": "accepted", "offset": self.manifest.offset, **self.manifest.public()}

    def finalize(self) -> dict[str, Any]:
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
        tmp.write_text(json.dumps(asdict(self.manifest), indent=2, sort_keys=True))
        tmp.replace(self.root / MANIFEST)
