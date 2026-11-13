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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.blueprint_draft import BlueprintDraft

# The unique index that enforces one row per (owner_id, title, version). A 23505 violation of
# THIS specific constraint is the only IntegrityError the version-allocation loop may retry.
_VERSION_UNIQUE_CONSTRAINT = "ix_blueprint_drafts_owner_title_version"

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
    extra_provenance: dict[str, Any] | None = None,
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
    provenance: dict[str, Any] = {
        "ai_generated": True,
        "originals_mutated": False,
        "draft": True,
        "pending_founder_approval": True,
        "grounded_in_sources": True,
        "source_count": len(source_document_ids),
    }
    if extra_provenance:
        # Caller-supplied, review-only signals (e.g. retrieved-content injection suspicion —
        # see routers/blueprint.py's module note) layered on top of the invariants above.
        # Never allowed to override them: a caller passing e.g. "pending_founder_approval"
        # here could not actually change the draft's real approval state (that is enforced in
        # the database, migrations 0071/0072 — see approve_blueprint_draft's docstring), but
        # this still keeps the recorded provenance honest rather than silently clobberable.
        provenance.update({k: v for k, v in extra_provenance.items() if k not in provenance})

    # P2 — version allocation is max(version)+1 guarded by the unique index
    # (owner_id, title, version). Two concurrent generations for the same title can compute the
    # same next version; one commits, the other hits a unique violation. Rather than surface a
    # raw 500, retry with a recomputed next version (a SAVEPOINT isolates each attempt so the
    # caller's transaction stays usable). Bounded so a genuine persistent conflict still errors.
    #
    # The retry is NARROW by design (examiner P2): it fires ONLY for a 23505 unique violation of
    # _VERSION_UNIQUE_CONSTRAINT. Any other IntegrityError — e.g. a missing-owner FK violation —
    # is a real error, not a version race; it is re-raised unchanged (never retried, never
    # relabelled as version_allocation_conflict), so it is never masked.
    last_exc: IntegrityError | None = None
    for _attempt in range(5):
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
            provenance=provenance,
        )
        try:
            with db.begin_nested():  # SAVEPOINT: a failed insert rolls back only this attempt
                db.add(row)
                db.flush()
            return row
        except IntegrityError as exc:
            orig = getattr(exc, "orig", None)
            pgcode = getattr(orig, "pgcode", None)
            constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
            if pgcode == "23505" and constraint == _VERSION_UNIQUE_CONSTRAINT:
                last_exc = exc  # concurrent same-version insert — recompute and retry
                continue
            raise  # any other IntegrityError is a real error, surfaced unchanged
    raise BlueprintError(
        "version_allocation_conflict: could not allocate a unique Blueprint version after retries"
    ) from last_exc


def approve_blueprint_draft(
    db: Session, *, owner_id: uuid.UUID, draft_id: uuid.UUID, approved_by: str
) -> BlueprintDraft:
    """Explicit Founder approval — the ONLY path from 'draft' to 'approved'.

    Integrity is enforced in the DATABASE, not here: approval runs through the owner-scoped,
    transaction-safe SECURITY DEFINER function `blueprint_approve_draft` (migrations 0071/0072),
    which takes a row lock (`FOR UPDATE`) and re-checks status inside the lock (concurrent
    approvals serialize), with a BEFORE UPDATE/DELETE guard trigger making an approved draft
    immutable and undeletable.

    Authority boundary (migration 0072, closing the GUC-spoofing P1): the function refuses to run
    when the caller's `session_user` is the runtime role `mainai_app`. `session_user` is the
    authenticated login role, which the runtime role cannot change (no SET ROLE/SESSION
    AUTHORIZATION privilege) — unlike the `app.current_user_id` GUC, which it CAN set. So a raw
    `mainai_app` connection, even controlling every custom GUC, cannot forge an approval. The
    legitimate approval must therefore run on a PRIVILEGED connection whose login role is not
    `mainai_app` — the app's admin/migration connection (`migration_engine`), the same privileged-
    session pattern app/worker.py uses for storage-deletion claims. `owner_id` is the authenticated
    Founder's id (the router passes `require_founder`'s `user.id`) — supplied by trusted server code
    on that privileged connection, never read from a caller-writable variable.

    The caller's `db` session (the ordinary request/runtime session) is used only to check
    existence for a clean 404 and to re-read the result."""
    from sqlalchemy import text as _sa_text

    from app.db import migration_engine

    row = db.get(BlueprintDraft, draft_id)
    if row is None or row.owner_id != owner_id:
        raise BlueprintError(f"blueprint draft {draft_id} not found for owner")

    try:
        # Privileged connection: session_user is the admin/migration role, not mainai_app, so the
        # function's authority guard admits it. set_config(..., true) scopes the owner GUC to this
        # one transaction only; owner_id is the server-authenticated Founder id, not caller input.
        with migration_engine.begin() as conn:
            conn.execute(
                _sa_text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(owner_id)},
            )
            conn.execute(
                _sa_text("SELECT blueprint_approve_draft(:draft_id, :approved_by)"),
                {"draft_id": str(draft_id), "approved_by": approved_by},
            )
    except Exception as exc:  # DB-enforced guard rejected it (not-found / not-draft / integrity)
        raise BlueprintError(f"blueprint draft {draft_id} could not be approved: {exc}") from exc

    db.expire(row)
    refreshed = db.get(BlueprintDraft, draft_id)
    if refreshed is None:  # pragma: no cover - approval just succeeded
        raise BlueprintError(f"blueprint draft {draft_id} vanished after approval")
    return refreshed


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
