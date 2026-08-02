"""Personal-data export domain service (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, PR
#31's Pass 26 account-integration slice) — the ONE place `app/routers/account.py`'s
`GET /api/account/export` delegates to, matching the shared-domain-service pattern already
established for source purging (app/rag/source_purge.py).

Pass 26 closes a real gap a founder review caught: the export used to say, in its own module
docstring, that "claims and generated analyses have no backing table yet" — stale since STEG
10 (KnowledgeClaim) and S1A (MemorySourceUnit/DocumentSourceUnit/MemorySourceLifecycleEvent)
both shipped. An account export that omits a person's own claims and provenance history is
not a complete personal-data export. This module adds all four S1A/claim tables, owner-scoped
and deterministically ordered, alongside the pre-existing account/conversation/knowledge
sections.

Deliberately still a structured JSON export, not a ZIP/binary bundle of the person's original
files — `Document.storage_key`/`ImportJob.source_storage_key` are recorded (nothing new here:
the pre-existing export already includes document metadata), but this endpoint does not read
their bytes back out of `app/storage/`. Widening this to include original file contents is an
explicit, separate, founder-approved slice — not assumed here.

Includes soft-deleted documents and purged/revoked memory source units: a GDPR-shaped export
must reflect what the system still holds about the person, not just what's currently visible
in the Library UI. A `purged` MemorySourceUnit's `content_text`/`content_hash` are correctly
`None` in the export (never fabricated to "fill the gap" — see
app/models/memory_source_unit.py's `SnapshotStatus` docstring for why that would be a lie
about what the system actually retains).

`export_schema_version` lets a future export format change be detected by whoever's reading
an old export file, instead of silently reinterpreting a differently-shaped payload.
`generated_at` timestamps the export itself, distinct from any row's own `created_at`.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.audit import AuditLog
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.import_job import ImportJob
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import DocumentSourceUnit, MemorySourceLifecycleEvent, MemorySourceUnit
from app.models.source_relationship import SourceRelationship
from app.models.user import User

EXPORT_SCHEMA_VERSION = 2  # 1 = pre-S1A (account/conversations/knowledge only); 2 = + claims/S1A provenance


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def export_account_data(db: Session, user: User, *, client_ip: str | None = None) -> dict:
    """Builds the full personal-data export for `user`, writes exactly one
    `account_data_exported` audit entry once the export object has been successfully
    assembled, and returns the export dict. Every section is filtered by `owner_id`/
    `uploaded_by`/`user_id` == `user.id` explicitly, not left to RLS alone — matching
    app/rag/source_purge.py's convention that a bug disabling RLS must still fail closed
    rather than silently widening what a query returns.

    Raises whatever the underlying queries raise (never partially writes the audit entry for
    a failed export — the audit write happens only after every section below has already
    succeeded). `client_ip` is a plain string, not a fastapi.Request, so this domain-layer
    module never imports fastapi — the router extracts it (see app/routers/account.py)."""
    owner_id = user.id

    conversations = db.query(Conversation).filter_by(user_id=owner_id).order_by(Conversation.created_at, Conversation.id).all()
    conversations_export = []
    for conversation in conversations:
        messages = (
            db.query(Message)
            .filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at, Message.id)
            .all()
        )
        conversations_export.append(
            {
                "id": str(conversation.id),
                "title": conversation.title,
                "created_at": _iso(conversation.created_at),
                "updated_at": _iso(conversation.updated_at),
                "messages": [
                    {
                        "role": m.role.value,
                        "content": m.content,
                        "provider": m.provider,
                        "model": m.model,
                        "created_at": _iso(m.created_at),
                    }
                    for m in messages
                ],
            }
        )

    audit_entries = db.query(AuditLog).filter_by(user_id=owner_id).order_by(AuditLog.created_at, AuditLog.id).all()

    # Founder Knowledge Studio material — includes soft-deleted sources too (deleted_at is
    # itself exported below) since a GDPR export must reflect what the system still holds
    # about the person, not just what's currently visible in the library UI.
    documents = db.query(Document).filter_by(uploaded_by=owner_id).order_by(Document.created_at, Document.id).all()
    document_ids = [d.id for d in documents]
    versions_by_source: dict[uuid.UUID, list] = {}
    if document_ids:
        for v in (
            db.query(KnowledgeVersion)
            .filter(KnowledgeVersion.source_id.in_(document_ids))
            .order_by(KnowledgeVersion.source_id, KnowledgeVersion.version_number)
            .all()
        ):
            versions_by_source.setdefault(v.source_id, []).append(v)

    documents_export = [
        {
            "id": str(d.id),
            "title": d.title,
            "source": d.source.value,
            "category": d.category,
            "classification": d.classification.value,
            "active_truth_status": d.active_truth_status.value,
            "media_type": d.media_type,
            "original_filename": d.original_filename,
            "checksum": d.checksum,
            "project_id": str(d.project_id) if d.project_id else None,
            "version_number": d.version_number,
            "status": d.status.value,
            "chunk_count": d.chunk_count,
            "media_duration_seconds": d.media_duration_seconds,
            "transcript_provider": d.transcript_provider,
            "imported_at": _iso(d.imported_at),
            "created_at": _iso(d.created_at),
            "deleted_at": _iso(d.deleted_at),
            "versions": [
                {
                    "version_number": v.version_number,
                    "checksum": v.checksum,
                    "extraction_version": v.extraction_version,
                    "created_at": _iso(v.created_at),
                }
                for v in versions_by_source.get(d.id, [])
            ],
        }
        for d in documents
    ]

    relationships_export = []
    if document_ids:
        relationships = (
            db.query(SourceRelationship)
            .filter(
                or_(
                    SourceRelationship.from_source_id.in_(document_ids),
                    SourceRelationship.to_source_id.in_(document_ids),
                )
            )
            .order_by(SourceRelationship.created_at, SourceRelationship.id)
            .all()
        )
        relationships_export = [
            {
                "from_source_id": str(r.from_source_id),
                "to_source_id": str(r.to_source_id),
                "relationship_type": r.relationship_type.value,
                "note": r.note,
                "created_at": _iso(r.created_at),
            }
            for r in relationships
        ]

    import_jobs = db.query(ImportJob).filter_by(owner_id=owner_id).order_by(ImportJob.created_at, ImportJob.id).all()
    import_jobs_export = [
        {
            "id": str(j.id),
            "status": j.status.value,
            "source_filename": j.source_filename,
            "project_id": str(j.project_id) if j.project_id else None,
            "succeeded_count": j.succeeded_count,
            "failed_count": j.failed_count,
            "skipped_count": j.skipped_count,
            "failure_reason": j.failure_reason,
            "created_at": _iso(j.created_at),
            "completed_at": _iso(j.completed_at),
        }
        for j in import_jobs
    ]

    # S1A provenance (Pass 26): owner-scoped, deterministically ordered by created_at then id
    # for reproducible exports/tests. A purged MemorySourceUnit's content_text/content_hash
    # are NULL in the DB already (see transition_own_memory_source's 'purged' branch,
    # migration 0019) — never recomputed or fabricated here.
    memory_source_units = (
        db.query(MemorySourceUnit).filter_by(owner_id=owner_id).order_by(MemorySourceUnit.created_at, MemorySourceUnit.id).all()
    )
    memory_source_units_export = [
        {
            "id": str(m.id),
            "source_kind": m.source_kind.value,
            "source_identity_key": m.source_identity_key,
            "source_role": m.source_role.value,
            "observed_at": _iso(m.observed_at),
            "occurred_at": _iso(m.occurred_at),
            "occurred_at_basis": m.occurred_at_basis.value,
            "content_text": m.content_text,
            "content_hash": m.content_hash,
            "content_hash_version": m.content_hash_version,
            "snapshot_status": m.snapshot_status.value,
            "lifecycle_status": m.lifecycle_status.value,
            "revoked_at": _iso(m.revoked_at),
            "revocation_reason": m.revocation_reason,
            "purged_at": _iso(m.purged_at),
            "purge_reason": m.purge_reason,
            "project_id": str(m.project_id) if m.project_id else None,
            "created_at": _iso(m.created_at),
        }
        for m in memory_source_units
    ]

    document_source_units = (
        db.query(DocumentSourceUnit)
        .filter_by(owner_id=owner_id)
        .order_by(DocumentSourceUnit.memory_source_id)
        .all()
    )
    document_source_units_export = [
        {
            "memory_source_id": str(dsu.memory_source_id),
            "document_id": str(dsu.document_id),
            "version_id": str(dsu.version_id) if dsu.version_id else None,
            "chunk_id": str(dsu.chunk_id) if dsu.chunk_id else None,
            "source_kind": dsu.source_kind.value,
        }
        for dsu in document_source_units
    ]

    lifecycle_events = (
        db.query(MemorySourceLifecycleEvent)
        .filter_by(owner_id=owner_id)
        .order_by(MemorySourceLifecycleEvent.created_at, MemorySourceLifecycleEvent.id)
        .all()
    )
    lifecycle_events_export = [
        {
            "id": str(e.id),
            "memory_source_id": str(e.memory_source_id),
            "from_status": e.from_status.value,
            "to_status": e.to_status.value,
            "reason": e.reason,
            "actor_type": e.actor_type,
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "created_at": _iso(e.created_at),
        }
        for e in lifecycle_events
    ]

    claims = db.query(KnowledgeClaim).filter_by(owner_id=owner_id).order_by(KnowledgeClaim.created_at, KnowledgeClaim.id).all()
    claims_export = [
        {
            "id": str(c.id),
            "source_id": str(c.source_id),
            "version_id": str(c.version_id) if c.version_id else None,
            "chunk_id": str(c.chunk_id) if c.chunk_id else None,
            "memory_source_id": str(c.memory_source_id) if c.memory_source_id else None,
            "project_id": str(c.project_id) if c.project_id else None,
            "claim_text": c.claim_text,
            "claim_type": c.claim_type.value,
            "status": c.status.value,
            "confidence": c.confidence.value,
            "grounding_score": c.grounding_score,
            "valid_from": _iso(c.valid_from),
            "valid_until": _iso(c.valid_until),
            "extraction_version": c.extraction_version,
            "created_at": _iso(c.created_at),
        }
        for c in claims
    ]

    export = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "id": str(user.id),
            "email": user.email,
            "role": user.role.value,
            "email_verified": user.email_verified,
            "created_at": _iso(user.created_at),
        },
        "conversations": conversations_export,
        "knowledge_sources": documents_export,
        "knowledge_source_relationships": relationships_export,
        "knowledge_import_jobs": import_jobs_export,
        "knowledge_claims": claims_export,
        "memory_source_units": memory_source_units_export,
        "document_source_units": document_source_units_export,
        "memory_source_lifecycle_events": lifecycle_events_export,
        "audit_log": [
            {
                "action": a.action,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "created_at": _iso(a.created_at),
            }
            for a in audit_entries
        ],
    }

    # Written only now that the export object above has been fully, successfully assembled —
    # an exception raised mid-collection never reaches this line, so a failed export can never
    # produce a false "account_data_exported" audit entry for data that was never actually
    # returned to the caller.
    record_audit(db, user_id=owner_id, action="account_data_exported", ip_address=client_ip)

    return export
