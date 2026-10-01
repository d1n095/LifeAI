"""Filesystem artifact identity observations; existence alone is never validity."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.base import ingest_observation, provider_failure
from app.claim_action_integrity.adapters.github import _bindings
from app.claim_action_integrity.adapters.types import EvidenceContext, FactMutability, ProviderObservation


class FilesystemEvidenceAdapter:
    def observe_artifact(self, db: Session, context: EvidenceContext, *, path: Path, expected_sha256: str | None = None):
        try:
            resolved = path.resolve()
            if not resolved.exists():
                observation = ProviderObservation(
                    "filesystem", f"filesystem:{resolved}", f"missing:{resolved}",
                    FactMutability.mutable_snapshot, _bindings(context, artifact_path=str(resolved)),
                    {"file_exists": False, "valid_artifact": False, "path": str(resolved)},
                    datetime.now(timezone.utc), 30,
                )
                return ingest_observation(db, context, observation)
            if not resolved.is_file():
                raise ValueError("artifact is not a regular file")
            digest = hashlib.sha256()
            size = 0
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            sha256 = digest.hexdigest()
            valid = expected_sha256 is not None and sha256 == expected_sha256
            stat = resolved.stat()
            facts = {"file_exists": True, "valid_artifact": valid, "sha256": sha256, "size": size, "path": str(resolved), "expected_sha256": expected_sha256}
            bindings = _bindings(context, artifact_sha256=sha256, artifact_path=str(resolved), artifact_identity=f"sha256:{sha256}:{size}")
            observation = ProviderObservation("filesystem", f"filesystem:{resolved}", f"stat:{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}", FactMutability.immutable_fact, bindings, facts, datetime.now(timezone.utc))
            return ingest_observation(db, context, observation)
        except Exception as exc:
            return provider_failure(exc)
