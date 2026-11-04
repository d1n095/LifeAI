"""Blueprint draft generation — Founder Alpha item 8/9.

Life synthesises an initial Blueprint DRAFT from the Founder's own indexed project materials.
Composition over existing infrastructure only (RAG retrieval for source-grounding + the active
local model for synthesis + the `blueprint_drafts` store) — no new retrieval/model/archive
system. Invariants:

  * The draft is ALWAYS created as `status="draft"`; promotion to `approved` is a SEPARATE,
    explicit Founder action (`approve_blueprint_draft`) — Life never self-approves.
  * The draft is TRACEABLE: `source_document_ids` / `source_refs` point at the immutable
    original Documents the retrieval returned. A draft with no sources is refused (never an
    ungrounded hallucination presented as a Blueprint).
  * The draft is an AI INTERPRETATION, stored separately from the originals; generating it
    never reads or mutates original bytes (`provenance.originals_mutated == False`).
  * Each regeneration for the same `title` is a new, immutable `version`.

`source_hits` are retrieval results (e.g. from `app.rag.vector_store.search`), passed in by the
caller so this service stays independent of which embedding space produced them. `synthesizer`
turns (title, query, source_hits) into draft text — in production it wraps the active local
model (`app.providers.registry.resolve_active(db, "chat")`); tests pass a deterministic stub for
the composition logic, and the real local-model synthesis is exercised end-to-end in CI.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.blueprint_draft import BlueprintDraft

Synthesizer = Callable[[str, str | None, list[dict[str, Any]]], str]


class BlueprintError(Exception):
    pass


def _excerpt(hit: dict[str, Any]) -> str:
    text = str(hit.get("text") or hit.get("excerpt") or hit.get("summary") or "")
    return text[:280]


def latest_version(db: Session, *, owner_id: uuid.UUID, title: str) -> int:
    rows = db.execute(
        select(BlueprintDraft.version).where(
            BlueprintDraft.owner_id == owner_id, BlueprintDraft.title == title
        )
    ).scalars().all()
    return max(rows) if rows else 0


def generate_blueprint_draft(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    source_hits: list[dict[str, Any]],
    synthesizer: Synthesizer,
    query: str | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
) -> BlueprintDraft:
    """Generate a new versioned, source-grounded Blueprint DRAFT. Refuses to create an
    ungrounded draft (no sources). Never mutates originals; never auto-approves."""
    hits = [h for h in (source_hits or []) if h.get("document_id")]
    if not hits:
        raise BlueprintError(
            "insufficient_sources: a Blueprint draft must be grounded in at least one indexed "
            "source document — refusing to generate an ungrounded draft"
        )

    body = synthesizer(title, query, hits)
    if not isinstance(body, str) or not body.strip():
        raise BlueprintError("synthesizer produced no draft text")

    source_document_ids = sorted({str(h["document_id"]) for h in hits})
    source_refs = [
        {"document_id": str(h["document_id"]), "similarity": h.get("similarity"), "excerpt": _excerpt(h)}
        for h in hits
    ]
    version = latest_version(db, owner_id=owner_id, title=title) + 1

    row = BlueprintDraft(
        owner_id=owner_id,
        title=title,
        version=version,
        status="draft",
        body=body,
        generated_from_query=query,
        source_document_ids=source_document_ids,
        source_refs=source_refs,
        model_provider=model_provider,
        model_name=model_name,
        provenance={
            "ai_generated": True,
            "originals_mutated": False,
            "draft": True,
            "pending_founder_approval": True,
            "grounded_in_sources": True,
            "source_count": len(source_document_ids),
        },
    )
    db.add(row)
    db.flush()
    return row


def approve_blueprint_draft(
    db: Session, *, owner_id: uuid.UUID, draft_id: uuid.UUID, approved_by: str
) -> BlueprintDraft:
    """Explicit Founder approval — the ONLY path from 'draft' to 'approved'. Separate from
    generation so an AI-generated draft is never treated as an approved decision."""
    from datetime import datetime

    row = db.get(BlueprintDraft, draft_id)
    if row is None or row.owner_id != owner_id:
        raise BlueprintError(f"blueprint draft {draft_id} not found for owner")
    if row.status != "draft":
        raise BlueprintError(f"blueprint draft {draft_id} is not in 'draft' status (is '{row.status}')")
    row.status = "approved"
    row.approved_at = datetime.utcnow()
    row.approved_by = approved_by
    prov = dict(row.provenance or {})
    prov["pending_founder_approval"] = False
    prov["approved_by_founder"] = True
    row.provenance = prov
    db.flush()
    return row


def list_blueprint_drafts(
    db: Session, *, owner_id: uuid.UUID, title: str | None = None
) -> list[BlueprintDraft]:
    stmt = select(BlueprintDraft).where(BlueprintDraft.owner_id == owner_id)
    if title is not None:
        stmt = stmt.where(BlueprintDraft.title == title)
    return list(db.execute(stmt.order_by(BlueprintDraft.title, BlueprintDraft.version)).scalars())


def get_blueprint_draft(db: Session, *, owner_id: uuid.UUID, draft_id: uuid.UUID) -> BlueprintDraft | None:
    row = db.get(BlueprintDraft, draft_id)
    if row is None or row.owner_id != owner_id:
        return None
    return row
