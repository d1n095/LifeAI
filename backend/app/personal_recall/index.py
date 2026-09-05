from __future__ import annotations

import json
import hashlib
import hmac
import os
import stat
import fcntl
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.personal_recall.types import DecisionState, IndexState, PersonalKnowledgeItem, Provenance, SourceAuthority, SourceType, VerificationState

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class RecallIndexError(ValueError):
    pass


class SnapshotStoragePolicy:
    """Mandatory trusted root for snapshots; callers cannot accidentally omit confinement."""

    def __init__(self, trusted_root: Path):
        root = Path(trusted_root)
        if not root.is_absolute() or not root.exists() or root.is_symlink():
            raise RecallIndexError("trusted snapshot root must be an existing absolute non-symlink directory")
        self.trusted_root = root.resolve(strict=True)

    def resolve(self, path: Path) -> Path:
        return _safe_path(path, base_dir=self.trusted_root)


class LocalRecallIndex:
    """Explicit local JSON snapshot. Raw content is optional and never sent anywhere."""

    FORMAT_VERSION = 1

    def __init__(self, *, owner_id: str | None = None, generation: int = 0) -> None:
        self.items: dict[str, PersonalKnowledgeItem] = {}
        self.owner_id = owner_id
        self.generation = generation

    def replace(self, items: list[PersonalKnowledgeItem]) -> None:
        owners = {item.owner_id for item in items}
        if len(owners) > 1 or (self.owner_id is not None and owners and owners != {self.owner_id}):
            raise RecallIndexError("snapshot cannot mix owners")
        if owners:
            self.owner_id = next(iter(owners))
        self.items = {item.item_id: item for item in items}

    def save(self, path: Path, *, policy: SnapshotStoragePolicy) -> None:
        path = policy.resolve(path)
        if self.owner_id is None:
            raise RecallIndexError("snapshot owner is required")
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with _snapshot_lock(lock_path, exclusive=True):
            self._save_locked(path)

    def _save_locked(self, path: Path) -> None:
        if path.exists() and path.is_symlink():
            raise RecallIndexError("snapshot path cannot be a symlink")
        disk_generation = _read_generation(path) if path.exists() else -1
        if disk_generation > self.generation:
            raise RecallIndexError("stale index cannot overwrite a newer generation")
        next_generation = max(self.generation, disk_generation) + 1
        body = {"format_version": self.FORMAT_VERSION, "owner_id": self.owner_id, "generation": next_generation, "items": [_encode(i) for i in self.items.values()]}
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload = {**body, "checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        if tmp.exists() and tmp.is_symlink():
            raise RecallIndexError("temporary snapshot path cannot be a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if len(encoded) > MAX_SNAPSHOT_BYTES:
                raise RecallIndexError("snapshot exceeds size limit")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RecallIndexError("snapshot write made no progress")
                view = view[written:]
            os.fsync(fd)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self.generation = next_generation

    @classmethod
    def load(cls, path: Path, *, owner_id: str | None = None, policy: SnapshotStoragePolicy) -> "LocalRecallIndex":
        path = policy.resolve(path)
        if path.is_symlink():
            raise RecallIndexError("snapshot path cannot be a symlink")
        with _snapshot_lock(path.with_suffix(path.suffix + ".lock"), exclusive=False):
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                raise RecallIndexError("snapshot exceeds size limit")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RecallIndexError("snapshot is unreadable or corrupt") from exc
        if payload.get("format_version") != cls.FORMAT_VERSION:
            raise RecallIndexError("unsupported recall index format")
        actual_owner = payload.get("owner_id")
        if not actual_owner or (owner_id is not None and actual_owner != owner_id):
            raise RecallIndexError("snapshot owner mismatch")
        checksum = payload.pop("checksum", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not checksum or not hmac.compare_digest(checksum, hashlib.sha256(canonical.encode("utf-8")).hexdigest()):
            raise RecallIndexError("snapshot integrity check failed")
        index = cls(owner_id=actual_owner, generation=int(payload.get("generation", 0)))
        index.replace([_decode(row) for row in payload["items"]])
        return index


def _encode(item: PersonalKnowledgeItem) -> dict:
    row = asdict(item)
    for key in ("source_type", "decision_state", "verification_state", "index_state", "source_authority"):
        row[key] = row[key].value
    row["provenance"]["source_type"] = row["provenance"]["source_type"].value
    for key in ("created_at", "updated_at", "valid_from", "valid_until"):
        row[key] = row[key].isoformat() if row[key] else None
    row["provenance"]["occurred_at"] = item.provenance.occurred_at.isoformat() if item.provenance.occurred_at else None
    return row


def _decode(row: dict) -> PersonalKnowledgeItem:
    data = dict(row)
    p = dict(data.pop("provenance"))
    p["source_type"] = SourceType(p["source_type"])
    p["occurred_at"] = datetime.fromisoformat(p["occurred_at"]) if p.get("occurred_at") else None
    data["provenance"] = Provenance(**p)
    data["source_type"] = SourceType(data["source_type"])
    data["decision_state"] = DecisionState(data["decision_state"])
    data["verification_state"] = VerificationState(data["verification_state"])
    data["index_state"] = IndexState(data["index_state"])
    data["source_authority"] = SourceAuthority(data.get("source_authority", "unknown"))
    for key in ("created_at", "updated_at", "valid_from", "valid_until"):
        data[key] = datetime.fromisoformat(data[key]) if data.get(key) else None
    data["entities"] = tuple(data.get("entities", ()))
    data["claims"] = tuple(data.get("claims", ()))
    data["aliases"] = tuple(data.get("aliases", ()))
    data["relationship_edges"] = {k: tuple(v) for k, v in data.get("relationship_edges", {}).items()}
    return PersonalKnowledgeItem(**data)


def _safe_path(path: Path, *, base_dir: Path | None) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise RecallIndexError("snapshot path must be absolute")
    if path.is_symlink():
        raise RecallIndexError("snapshot path cannot be a symlink")
    resolved = path.resolve(strict=False)
    if base_dir is not None:
        base = Path(base_dir).resolve(strict=True)
        if resolved.parent != base and base not in resolved.parents:
            raise RecallIndexError("snapshot path escapes its base directory")
    return resolved


def _read_generation(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload.get("generation", -1))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RecallIndexError("existing snapshot is corrupt; refusing overwrite")


@contextmanager
def _snapshot_lock(path: Path, *, exclusive: bool):
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RecallIndexError("cannot establish snapshot lock") from exc
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecallIndexError("snapshot operation already in progress") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
