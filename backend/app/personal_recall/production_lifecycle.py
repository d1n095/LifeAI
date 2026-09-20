from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.models.personal_recall_production import (
    PersonalRecallChunk,
    PersonalRecallExtraction,
    PersonalRecallGrant,
    PersonalRecallOwnerKey,
    PersonalRecallSource,
)
from app.personal_recall.production_crypto import RecallCryptoError, SystemKEK, decrypt_aead, load_system_kek_from_env
from app.personal_recall.production_ingestion import _aad, _current_rls_owner


@dataclass(frozen=True)
class PersonalRecallErasureResult:
    sources_scrubbed: int
    chunks_deleted: int
    grants_deleted: int
    extractions_deleted: int
    owner_keys_deleted: int


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _owner_key_material(key_row: PersonalRecallOwnerKey, *, system_kek: SystemKEK) -> bytes:
    if key_row.revoked_at is not None or key_row.status not in {"active", "rotated"}:
        raise RecallCryptoError("owner recall key is not usable for export")
    if key_row.system_kek_version != system_kek.version:
        raise RecallCryptoError("wrong system KEK version")
    return decrypt_aead(
        system_kek.key,
        key_row.wrap_nonce,
        key_row.wrapped_owner_key,
        _aad("owner-key", key_row.owner_id, key_row.key_version, key_row.system_kek_version),
    )


def export_personal_recall_data(db: Session, *, owner_id: uuid.UUID) -> dict:
    """Owner-scoped Personal Recall export.

    Source/chunk text is user content and may be exported when the configured KEK can unwrap
    the owner's relevant key version. Key material itself is never exported: only status,
    algorithm, version and lifecycle metadata are included.
    """
    key_rows = (
        db.execute(
            select(PersonalRecallOwnerKey)
            .where(PersonalRecallOwnerKey.owner_id == owner_id)
            .order_by(PersonalRecallOwnerKey.key_version)
        )
        .scalars()
        .all()
    )
    sources = (
        db.execute(
            select(PersonalRecallSource)
            .where(PersonalRecallSource.owner_id == owner_id)
            .order_by(PersonalRecallSource.created_at, PersonalRecallSource.id)
        )
        .scalars()
        .all()
    )
    chunks = (
        db.execute(
            select(PersonalRecallChunk)
            .where(PersonalRecallChunk.owner_id == owner_id)
            .order_by(PersonalRecallChunk.source_id, PersonalRecallChunk.chunk_index, PersonalRecallChunk.id)
        )
        .scalars()
        .all()
    )
    grants = (
        db.execute(
            select(PersonalRecallGrant)
            .where(PersonalRecallGrant.owner_id == owner_id)
            .order_by(PersonalRecallGrant.created_at, PersonalRecallGrant.id)
        )
        .scalars()
        .all()
    )
    extractions = (
        db.execute(
            select(PersonalRecallExtraction)
            .where(PersonalRecallExtraction.owner_id == owner_id)
            .order_by(PersonalRecallExtraction.created_at, PersonalRecallExtraction.id)
        )
        .scalars()
        .all()
    )

    key_material_by_version: dict[int, bytes] = {}
    decryption_errors: dict[int, str] = {}
    if key_rows:
        try:
            system_kek = load_system_kek_from_env()
        except Exception as exc:  # fail truthful in export rather than pretending content is absent
            system_kek = None
            for key in key_rows:
                decryption_errors[key.key_version] = type(exc).__name__
        if system_kek is not None:
            for key in key_rows:
                try:
                    key_material_by_version[key.key_version] = _owner_key_material(key, system_kek=system_kek)
                except Exception as exc:
                    decryption_errors[key.key_version] = type(exc).__name__

    def _decrypt_payload(*, key_version: int, nonce: bytes, ciphertext: bytes, aad: bytes) -> tuple[str | None, str | None]:
        key = key_material_by_version.get(key_version)
        if key is None:
            return None, decryption_errors.get(key_version, "owner_key_unavailable")
        try:
            return decrypt_aead(key, nonce, ciphertext, aad).decode("utf-8"), None
        except Exception as exc:
            return None, type(exc).__name__

    source_exports = []
    for source in sources:
        text_value = None
        decryption_error = None
        if source.state != "deleted" and source.encrypted_payload:
            text_value, decryption_error = _decrypt_payload(
                key_version=source.owner_key_version,
                nonce=source.payload_nonce,
                ciphertext=source.encrypted_payload,
                aad=_aad("source", owner_id, source.source_hash, source.owner_key_version),
            )
        source_exports.append(
            {
                "id": str(source.id),
                "source_identity": source.source_identity,
                "filename": source.filename,
                "logical_path": source.logical_path,
                "source_hash": source.source_hash,
                "content_hash": source.content_hash,
                "media_type": source.media_type,
                "parser_version": source.parser_version,
                "state": source.state,
                "disclosure_class": source.disclosure_class,
                "owner_key_version": source.owner_key_version,
                "supersedes_source_id": str(source.supersedes_source_id) if source.supersedes_source_id else None,
                "created_at": _iso(source.created_at),
                "deleted_at": _iso(source.deleted_at),
                "revoked_at": _iso(source.revoked_at),
                "metadata": source.metadata_json,
                "text": text_value,
                "decryption_error": decryption_error,
            }
        )

    chunk_exports = []
    for chunk in chunks:
        text_value = None
        decryption_error = None
        if chunk.state != "deleted" and chunk.encrypted_text:
            text_value, decryption_error = _decrypt_payload(
                key_version=chunk.owner_key_version,
                nonce=chunk.payload_nonce,
                ciphertext=chunk.encrypted_text,
                aad=_aad("chunk", owner_id, chunk.source_id, chunk.chunk_index, chunk.content_hash),
            )
        chunk_exports.append(
            {
                "id": str(chunk.id),
                "source_id": str(chunk.source_id),
                "chunk_index": chunk.chunk_index,
                "chunk_identity": chunk.chunk_identity,
                "content_hash": chunk.content_hash,
                "classification": chunk.classification,
                "state": chunk.state,
                "owner_key_version": chunk.owner_key_version,
                "created_at": _iso(chunk.created_at),
                "metadata": chunk.metadata_json,
                "text": text_value,
                "decryption_error": decryption_error,
            }
        )

    return {
        "sources": source_exports,
        "chunks": chunk_exports,
        "extractions": [
            {
                "id": str(row.id),
                "source_id": str(row.source_id),
                "chunk_id": str(row.chunk_id) if row.chunk_id else None,
                "kind": row.kind,
                "text_hash": row.text_hash,
                "confidence": row.confidence,
                "state": row.state,
                "provenance": row.provenance,
                "created_at": _iso(row.created_at),
            }
            for row in extractions
        ],
        "grants": [
            {
                "id": str(row.id),
                "session_id": row.session_id,
                "boot_id": row.boot_id,
                "purpose": row.purpose,
                "scope": row.scope,
                "resource_classes": row.resource_classes,
                "disclosure_level": row.disclosure_level,
                "can_retrieve": row.can_retrieve,
                "can_disclose": row.can_disclose,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
                "expires_at": _iso(row.expires_at),
                "revoked_at": _iso(row.revoked_at),
            }
            for row in grants
        ],
        "owner_keys": [
            {
                "id": str(row.id),
                "key_version": row.key_version,
                "status": row.status,
                "wrap_algorithm": row.wrap_algorithm,
                "system_kek_version": row.system_kek_version,
                "created_at": _iso(row.created_at),
                "rotated_at": _iso(row.rotated_at),
                "revoked_at": _iso(row.revoked_at),
                "key_material_exported": False,
            }
            for row in key_rows
        ],
    }


def erase_personal_recall_data(db: Session, *, owner_id: uuid.UUID) -> PersonalRecallErasureResult:
    """Governed account-erasure path for Personal Recall production data.

    This is intentionally not ordinary table DELETE access. The account-erasure transaction
    must already be bound to the owner via RLS/session state. Content ciphertext is scrubbed
    before parent rows are removed, grants are revoked, and owner-key/grant deletes are
    allowed only under the narrow erasure GUC consumed by the DB triggers.
    """
    if _current_rls_owner(db) != owner_id:
        raise ValueError("personal recall erasure requires the current owner DB session")

    now = datetime.now(timezone.utc)
    db.execute(sql_text("SET LOCAL app.personal_recall_erasure_in_progress = 'true'"))
    sources = db.execute(select(PersonalRecallSource).where(PersonalRecallSource.owner_id == owner_id)).scalars().all()
    for source in sources:
        source.state = "deleted"
        source.encrypted_payload = b""
        source.payload_nonce = b""
        source.deleted_at = now
        metadata = dict(source.metadata_json or {})
        metadata["erased_by_account_erasure"] = True
        source.metadata_json = metadata

    extractions_deleted = db.query(PersonalRecallExtraction).filter_by(owner_id=owner_id).delete(synchronize_session=False)
    chunks_deleted = db.query(PersonalRecallChunk).filter_by(owner_id=owner_id).delete(synchronize_session=False)

    for grant in db.execute(select(PersonalRecallGrant).where(PersonalRecallGrant.owner_id == owner_id)).scalars().all():
        grant.can_retrieve = False
        grant.can_disclose = False
        grant.revoked_at = grant.revoked_at or now

    for key in db.execute(select(PersonalRecallOwnerKey).where(PersonalRecallOwnerKey.owner_id == owner_id)).scalars().all():
        key.status = "revoked"
        key.revoked_at = key.revoked_at or now

    db.flush()
    grants_deleted = db.query(PersonalRecallGrant).filter_by(owner_id=owner_id).delete(synchronize_session=False)
    owner_keys_deleted = db.query(PersonalRecallOwnerKey).filter_by(owner_id=owner_id).delete(synchronize_session=False)
    sources_deleted = db.query(PersonalRecallSource).filter_by(owner_id=owner_id).delete(synchronize_session=False)
    return PersonalRecallErasureResult(
        sources_scrubbed=sources_deleted or len(sources),
        chunks_deleted=chunks_deleted,
        grants_deleted=grants_deleted,
        extractions_deleted=extractions_deleted,
        owner_keys_deleted=owner_keys_deleted,
    )
