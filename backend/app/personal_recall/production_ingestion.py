from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.models.personal_recall_production import (
    PersonalRecallChunk,
    PersonalRecallExtraction,
    PersonalRecallGrant,
    PersonalRecallOwnerKey,
    PersonalRecallSource,
)
from app.personal_recall.production_crypto import ALGORITHM, RecallCryptoError, SystemKEK, decrypt_aead, encrypt_aead, generate_key

PARSER_VERSION = "personal-recall-file-ingestion-v1"
MAX_FILE_BYTES = 1_000_000
CHUNK_CHARS = 1800
SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".pdf"}
CLASSIFICATION_KINDS = ("IDEA", "DREAM", "QUESTION", "PLAN", "DECISION", "REQUIREMENT", "TASK", "EVIDENCE", "IMPLEMENTATION", "VERIFIED_RESULT", "REJECTED", "SUPERSEDED", "UNKNOWN")


class RecallProductionError(ValueError):
    pass


@dataclass(frozen=True)
class IngestedSourceResult:
    source_id: uuid.UUID
    source_hash: str
    content_hash: str
    chunk_ids: tuple[uuid.UUID, ...]
    duplicate: bool
    extraction_kinds: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalGrantContext:
    owner_id: uuid.UUID
    session_id: str
    purpose: str
    resource_classes: tuple[str, ...]
    disclosure_level: str
    can_disclose: bool


@dataclass(frozen=True)
class RetrievedChunk:
    source_id: uuid.UUID
    chunk_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str | None
    disclosure_level: str
    provenance: dict
    classification: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aad(*parts: object) -> bytes:
    return "|".join(str(part) for part in parts).encode("utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_guard(db: Session, name: str, value: str) -> None:
    db.execute(sql_text(f"SET LOCAL {name} = :value"), {"value": value})


def get_or_create_owner_key(db: Session, *, owner_id: uuid.UUID, system_kek: SystemKEK) -> PersonalRecallOwnerKey:
    row = db.execute(
        select(PersonalRecallOwnerKey)
        .where(PersonalRecallOwnerKey.owner_id == owner_id, PersonalRecallOwnerKey.status == "active")
        .order_by(PersonalRecallOwnerKey.key_version.desc())
    ).scalars().first()
    if row is not None:
        return row
    owner_key = generate_key()
    nonce, wrapped = encrypt_aead(system_kek.key, owner_key, _aad("owner-key", owner_id, 1, system_kek.version))
    _set_guard(db, "app.personal_recall_key_authority", "founder_authorized")
    row = PersonalRecallOwnerKey(
        owner_id=owner_id,
        key_version=1,
        status="active",
        wrap_algorithm=ALGORITHM,
        system_kek_version=system_kek.version,
        wrap_nonce=nonce,
        wrapped_owner_key=wrapped,
    )
    db.add(row)
    db.flush()
    return row


def unwrap_owner_key(row: PersonalRecallOwnerKey, *, system_kek: SystemKEK) -> bytes:
    if row.status != "active" or row.revoked_at is not None:
        raise RecallCryptoError("owner recall key is not active")
    if row.system_kek_version != system_kek.version:
        raise RecallCryptoError("wrong system KEK version")
    return decrypt_aead(system_kek.key, row.wrap_nonce, row.wrapped_owner_key, _aad("owner-key", row.owner_id, row.key_version, row.system_kek_version))


def rotate_owner_key(db: Session, *, owner_id: uuid.UUID, system_kek: SystemKEK) -> PersonalRecallOwnerKey:
    current = get_or_create_owner_key(db, owner_id=owner_id, system_kek=system_kek)
    _set_guard(db, "app.personal_recall_key_authority", "founder_authorized")
    current.status = "rotated"
    current.rotated_at = _now()
    new_version = current.key_version + 1
    owner_key = generate_key()
    nonce, wrapped = encrypt_aead(system_kek.key, owner_key, _aad("owner-key", owner_id, new_version, system_kek.version))
    row = PersonalRecallOwnerKey(
        owner_id=owner_id,
        key_version=new_version,
        status="active",
        wrap_algorithm=ALGORITHM,
        system_kek_version=system_kek.version,
        wrap_nonce=nonce,
        wrapped_owner_key=wrapped,
    )
    db.add(row)
    db.flush()
    return row


def create_recall_grant(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str,
    purpose: str,
    resource_classes: Iterable[str],
    disclosure_level: str = "snippet",
    ttl_seconds: int = 900,
    can_disclose: bool = True,
    boot_id: str | None = None,
    created_by: str = "founder",
) -> PersonalRecallGrant:
    if ttl_seconds <= 0 or ttl_seconds > 86400:
        raise RecallProductionError("grant ttl must be bounded")
    if disclosure_level not in {"metadata", "snippet", "none"}:
        raise RecallProductionError("invalid disclosure level")
    if created_by != "founder":
        raise RecallProductionError("ordinary MainAI runtime cannot self-grant recall access")
    classes = sorted(set(resource_classes))
    if not classes:
        raise RecallProductionError("grant requires at least one resource class")
    _set_guard(db, "app.personal_recall_grant_authority", "founder_authorized")
    row = PersonalRecallGrant(
        owner_id=owner_id,
        session_id=session_id,
        boot_id=boot_id,
        purpose=purpose,
        scope={"owner_id": str(owner_id)},
        resource_classes=classes,
        disclosure_level=disclosure_level,
        can_retrieve=True,
        can_disclose=can_disclose,
        created_by=created_by,
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )
    db.add(row)
    db.flush()
    return row


def _active_grant(db: Session, *, owner_id: uuid.UUID, session_id: str, purpose: str, resource_class: str) -> PersonalRecallGrant:
    row = db.execute(
        select(PersonalRecallGrant)
        .where(
            PersonalRecallGrant.owner_id == owner_id,
            PersonalRecallGrant.session_id == session_id,
            PersonalRecallGrant.purpose == purpose,
            PersonalRecallGrant.can_retrieve.is_(True),
            PersonalRecallGrant.revoked_at.is_(None),
            PersonalRecallGrant.expires_at > _now(),
        )
        .order_by(PersonalRecallGrant.created_at.desc())
    ).scalars().first()
    if row is None or resource_class not in row.resource_classes:
        raise RecallProductionError("valid recall grant is required")
    return row


def _parse_text(filename: str, payload: bytes) -> tuple[str, str]:
    lower = filename.lower()
    if len(payload) > MAX_FILE_BYTES:
        raise RecallProductionError("file exceeds bounded ingestion size")
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ".txt"
    if ext not in SUPPORTED_EXTENSIONS:
        raise RecallProductionError("unsupported file type")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(payload))
            return "\n".join(page.extract_text() or "" for page in reader.pages), "application/pdf"
        except Exception as exc:
            raise RecallProductionError("pdf text extraction failed") from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecallProductionError("file must be valid UTF-8 text for this ingestion foundation") from exc
    if ext == ".json":
        json.loads(text)
        return text, "application/json"
    return text, "text/markdown" if ext == ".md" else "text/plain"


def _chunks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    return [normalized[i:i + CHUNK_CHARS] for i in range(0, len(normalized), CHUNK_CHARS)] or [""]


def _classify(text: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("must", "shall", "requirement", "krav")):
        return "REQUIREMENT"
    if any(token in lower for token in ("decision", "beslut", "we chose")):
        return "DECISION"
    if any(token in lower for token in ("todo", "task", "next step")):
        return "TASK"
    if any(token in lower for token in ("verified", "passed", "green")):
        return "VERIFIED_RESULT"
    if "?" in text:
        return "QUESTION"
    if any(token in lower for token in ("idea", "maybe", "could", "dream")):
        return "IDEA"
    return "UNKNOWN"


def ingest_file(
    db: Session,
    *,
    owner_id: uuid.UUID,
    filename: str,
    payload: bytes,
    system_kek: SystemKEK,
    session_id: str,
    purpose: str = "founder_file_ingestion",
    logical_path: str | None = None,
    disclosure_class: str = "normal",
) -> IngestedSourceResult:
    _active_grant(db, owner_id=owner_id, session_id=session_id, purpose=purpose, resource_class="file")
    text, media_type = _parse_text(filename, payload)
    source_hash = _sha256(payload)
    content_hash = _sha256(text.encode("utf-8"))
    existing = db.execute(select(PersonalRecallSource).where(PersonalRecallSource.owner_id == owner_id, PersonalRecallSource.source_hash == source_hash)).scalar_one_or_none()
    if existing is not None:
        chunks = db.execute(select(PersonalRecallChunk.id).where(PersonalRecallChunk.owner_id == owner_id, PersonalRecallChunk.source_id == existing.id).order_by(PersonalRecallChunk.chunk_index)).scalars().all()
        return IngestedSourceResult(existing.id, source_hash, content_hash, tuple(chunks), True, tuple())
    key_row = get_or_create_owner_key(db, owner_id=owner_id, system_kek=system_kek)
    owner_key = unwrap_owner_key(key_row, system_kek=system_kek)
    source_identity = f"file:{source_hash}"
    source_nonce, encrypted_payload = encrypt_aead(owner_key, text.encode("utf-8"), _aad("source", owner_id, source_hash, key_row.key_version))
    source = PersonalRecallSource(
        owner_id=owner_id,
        source_identity=source_identity,
        filename=filename,
        logical_path=logical_path,
        source_hash=source_hash,
        content_hash=content_hash,
        media_type=media_type,
        parser_version=PARSER_VERSION,
        state="active",
        disclosure_class=disclosure_class,
        encrypted_payload=encrypted_payload,
        payload_algorithm=ALGORITHM,
        payload_nonce=source_nonce,
        owner_key_version=key_row.key_version,
        metadata_json={"source_unit_model": "personal_recall_source_v1", "file_content_is_authority": False},
    )
    db.add(source)
    db.flush()
    chunk_ids: list[uuid.UUID] = []
    kinds: list[str] = []
    for idx, chunk_text in enumerate(_chunks(text)):
        chunk_hash = _sha256(chunk_text.encode("utf-8"))
        nonce, encrypted = encrypt_aead(owner_key, chunk_text.encode("utf-8"), _aad("chunk", owner_id, source.id, idx, chunk_hash))
        kind = _classify(chunk_text)
        kinds.append(kind)
        chunk = PersonalRecallChunk(
            owner_id=owner_id,
            source_id=source.id,
            chunk_index=idx,
            chunk_identity=f"{source_hash}:{idx}:{chunk_hash}",
            content_hash=chunk_hash,
            encrypted_text=encrypted,
            payload_algorithm=ALGORITHM,
            payload_nonce=nonce,
            owner_key_version=key_row.key_version,
            classification=kind,
            state="active",
            metadata_json={"original_order": idx, "parser_version": PARSER_VERSION, "embedded_instruction_is_authority": False},
        )
        db.add(chunk)
        db.flush()
        chunk_ids.append(chunk.id)
        db.add(PersonalRecallExtraction(owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, kind=kind, text_hash=chunk_hash, confidence=0.25 if kind == "UNKNOWN" else 0.6, state="proposed", provenance={"source_hash": source_hash, "chunk_index": idx}))
    db.flush()
    return IngestedSourceResult(source.id, source_hash, content_hash, tuple(chunk_ids), False, tuple(sorted(set(kinds))))


def retrieve_chunks(
    db: Session,
    *,
    owner_id: uuid.UUID,
    query: str,
    system_kek: SystemKEK,
    grant: RetrievalGrantContext,
    source_id: uuid.UUID | None = None,
    include_deleted: bool = False,
) -> list[RetrievedChunk]:
    row = _active_grant(db, owner_id=owner_id, session_id=grant.session_id, purpose=grant.purpose, resource_class="file")
    if row.disclosure_level == "none" or not row.can_disclose or not grant.can_disclose:
        disclose_text = False
    else:
        disclose_text = row.disclosure_level == "snippet" and grant.disclosure_level == "snippet"
    if row.owner_id != owner_id:
        raise RecallProductionError("grant owner mismatch")
    key_row = get_or_create_owner_key(db, owner_id=owner_id, system_kek=system_kek)
    owner_key = unwrap_owner_key(key_row, system_kek=system_kek)
    terms = [t for t in re.findall(r"[\wåäöÅÄÖ]+", query.lower()) if len(t) > 1]
    stmt = select(PersonalRecallChunk, PersonalRecallSource).join(PersonalRecallSource, PersonalRecallSource.id == PersonalRecallChunk.source_id).where(PersonalRecallChunk.owner_id == owner_id, PersonalRecallSource.owner_id == owner_id)
    if source_id is not None:
        stmt = stmt.where(PersonalRecallSource.id == source_id)
    if not include_deleted:
        stmt = stmt.where(PersonalRecallChunk.state == "active", PersonalRecallSource.state == "active")
    rows = db.execute(stmt.order_by(PersonalRecallSource.created_at, PersonalRecallChunk.chunk_index)).all()
    results: list[RetrievedChunk] = []
    for chunk, source in rows:
        plaintext = decrypt_aead(owner_key, chunk.payload_nonce, chunk.encrypted_text, _aad("chunk", owner_id, source.id, chunk.chunk_index, chunk.content_hash)).decode("utf-8")
        haystack = f"{source.filename}\n{plaintext}".lower()
        if terms and not any(term in haystack for term in terms):
            continue
        provenance = {"source_id": str(source.id), "source_hash": source.source_hash, "content_hash": chunk.content_hash, "filename": source.filename, "state": source.state, "parser_version": source.parser_version}
        results.append(RetrievedChunk(source.id, chunk.id, source.filename, chunk.chunk_index, plaintext[:500] if disclose_text else None, row.disclosure_level, provenance, chunk.classification))
    return results


def revoke_source(db: Session, *, owner_id: uuid.UUID, source_id: uuid.UUID, reason: str = "founder_revoked") -> None:
    source = db.execute(select(PersonalRecallSource).where(PersonalRecallSource.id == source_id, PersonalRecallSource.owner_id == owner_id)).scalar_one_or_none()
    if source is None:
        raise RecallProductionError("source not found")
    source.state = "revoked"
    source.revoked_at = _now()
    source.metadata_json = {**(source.metadata_json or {}), "revocation_reason": reason}
    chunks = db.execute(select(PersonalRecallChunk).where(PersonalRecallChunk.owner_id == owner_id, PersonalRecallChunk.source_id == source_id)).scalars().all()
    for chunk in chunks:
        chunk.state = "revoked"


def delete_source(db: Session, *, owner_id: uuid.UUID, source_id: uuid.UUID, reason: str = "founder_deleted") -> None:
    source = db.execute(select(PersonalRecallSource).where(PersonalRecallSource.id == source_id, PersonalRecallSource.owner_id == owner_id)).scalar_one_or_none()
    if source is None:
        raise RecallProductionError("source not found")
    source.state = "deleted"
    source.deleted_at = _now()
    source.encrypted_payload = b""
    source.payload_nonce = b""
    source.metadata_json = {"deletion_reason": reason, "source_hash": source.source_hash}
    chunks = db.execute(select(PersonalRecallChunk).where(PersonalRecallChunk.owner_id == owner_id, PersonalRecallChunk.source_id == source_id)).scalars().all()
    for chunk in chunks:
        chunk.state = "deleted"
        chunk.encrypted_text = b""
        chunk.payload_nonce = b""
