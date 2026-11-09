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
    open_questions: list[str] | None = None,
) -> BlueprintDraft:
    """Generate a new versioned, source-grounded Blueprint DRAFT. Refuses to create an
    ungrounded draft (no sources). Never mutates originals; never auto-approves.

    `open_questions` lets the synthesis step surface the Blueprint's own MISSING INFORMATION /
    unresolved dependencies (Founder-Alpha Blueprint field "Missing information"). It is recorded
    in `provenance` (a JSON column — no schema change) so a draft honestly carries what it could
    NOT yet ground, rather than presenting a confident-looking but incomplete plan. Optional and
    backward-compatible: omitting it preserves the exact prior behavior."""
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
    # Grounding BASELINE: capture each source's content identity (checksum) and truth-status AT
    # generation time, so a later readiness/approval check can tell a STATUS change
    # (active -> historical) apart from a CONTENT/VERSION change (checksum differs) — both
    # invalidate the original grounding. Additive to source_refs (a JSON column); no schema change.
    _baseline = _source_baselines(db, source_document_ids)
    source_refs = [
        {
            "document_id": str(h["document_id"]),
            "similarity": h.get("similarity"),
            "excerpt": _excerpt(h),
            "source_checksum": _baseline.get(str(h["document_id"]), {}).get("checksum"),
            "source_status_at_generation": _baseline.get(str(h["document_id"]), {}).get("status"),
        }
        for h in hits
    ]
    provenance = {
        "ai_generated": True,
        "originals_mutated": False,
        "draft": True,
        "pending_founder_approval": True,
        "grounded_in_sources": True,
        "source_count": len(source_document_ids),
        "open_questions": list(open_questions or []),
        "has_open_questions": bool(open_questions),
    }

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

    Source-readiness is ENFORCED here, server-side and fail-closed: inside the privileged
    transaction the draft row is locked FOR UPDATE and its grounding evidence is re-checked FRESH
    (status change OR content/checksum change OR missing/unauthorized source). A STALE/BLOCKED
    draft is refused and nothing is approved. This closes the gap where staleness was detectable
    (`blueprint_source_staleness` / `evaluate_blueprint_readiness`) but not prevented — a readiness
    report alone never authorizes.

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
            # SERVER-ENFORCED SOURCE-READINESS GATE (fail closed), race-safe by construction:
            # lock the draft row FOR UPDATE *before* re-checking its evidence, so no concurrent
            # approval can slip between this check and the status flip (TOCTOU), and the evidence
            # is re-read FRESH inside this transaction — not trusted from generation time. If any
            # grounding source is now stale (status change OR content/checksum change) or
            # missing/unauthorized, the whole transaction aborts and NOTHING is approved. A
            # successful readiness report never silently authorizes: approval still runs this gate.
            locked = conn.execute(
                _sa_text(
                    "SELECT 1 FROM blueprint_drafts "
                    "WHERE id = :id AND owner_id = :owner AND status = 'draft' FOR UPDATE"
                ),
                {"id": str(draft_id), "owner": str(owner_id)},
            ).first()
            if locked is None:
                raise BlueprintError(
                    f"blueprint draft {draft_id} is not an approvable draft for this owner "
                    "(missing, not owned, or already decided)"
                )
            recorded = {
                str(r.get("document_id")): r.get("source_checksum") for r in (row.source_refs or [])
            }
            severities: list[str | None] = []
            reasons: list[str] = []
            for sid in (str(s) for s in (row.source_document_ids or [])):
                cur = conn.execute(
                    _sa_text(
                        "SELECT uploaded_by::text AS uploaded_by, "
                        "active_truth_status::text AS status, checksum "
                        "FROM documents WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": sid},
                ).mappings().first()
                present = cur is not None
                authorized = bool(present and cur["uploaded_by"] == str(owner_id))
                severity, reason = _classify_source(
                    present=present,
                    authorized=authorized,
                    status=cur["status"] if present else None,
                    recorded_checksum=recorded.get(sid),
                    current_checksum=cur["checksum"] if present else None,
                )
                severities.append(severity)
                if reason:
                    reasons.append(f"{sid}:{reason}")
            open_questions = list((row.provenance or {}).get("open_questions") or [])
            readiness = _overall_readiness(severities, bool(open_questions))
            if readiness in APPROVAL_BLOCKING_READINESS:
                raise BlueprintError(
                    f"approval_blocked_{readiness.lower()}: Blueprint evidence is not approvable "
                    f"({readiness}); refusing to approve on stale/missing/unauthorized sources: "
                    + (", ".join(reasons) or "see readiness report")
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


def blueprint_source_staleness(
    db: Session, *, owner_id: uuid.UUID, draft: BlueprintDraft
) -> dict[str, Any]:
    """Read-only staleness check for a draft's grounding EVIDENCE.

    A Blueprint is only as current as the sources it was grounded in. After a draft is generated,
    a source Document can be reclassified by the Founder as historical/superseded/disputed/proposed
    (`ActiveTruthStatus`), or removed. This reports which of `draft.source_document_ids` are no
    longer current, so a stale Blueprint is detectable BEFORE it is approved or acted on — directly
    serving the Founder quality requirement "detect stale decisions and source evidence."

    Reuses `app.rag.trust.NON_ACTIVE_STATUSES` + `ActiveTruthStatus` (no second staleness notion).
    Owner-scoped (documents are owner-scoped via `uploaded_by`), never mutates anything, and adds
    no migration — it only reads the current classification of already-recorded sources.
    """
    from app.models.document import Document
    from app.rag.trust import NON_ACTIVE_STATUSES

    details: list[dict[str, Any]] = []
    for sid in draft.source_document_ids or []:
        try:
            doc = db.get(Document, uuid.UUID(str(sid)))
        except (ValueError, TypeError):
            details.append({"document_id": str(sid), "reason": "invalid_id"})
            continue
        if doc is None or doc.uploaded_by != owner_id:
            # Missing, or not the owner's own source -> cannot be treated as current evidence.
            details.append({"document_id": str(sid), "reason": "missing"})
            continue
        status = getattr(doc.active_truth_status, "value", str(doc.active_truth_status))
        if status in NON_ACTIVE_STATUSES:
            details.append({"document_id": str(sid), "reason": status})

    return {
        "draft_id": str(draft.id),
        "is_stale": bool(details),
        "stale_source_ids": [d["document_id"] for d in details],
        "details": details,
    }


# --- Blueprint readiness + fail-closed approval safety -----------------------------------------

READINESS_READY = "READY"
READINESS_NEEDS_INFORMATION = "NEEDS_INFORMATION"
READINESS_STALE = "STALE"
READINESS_BLOCKED = "BLOCKED"

# Approval FAILS CLOSED on these: stale/missing/unauthorized EVIDENCE. NEEDS_INFORMATION
# (unanswered open questions) is advisory — the Founder may knowingly approve despite it — so it
# does NOT block approval. READY/NEEDS_INFORMATION are approvable; STALE/BLOCKED are not.
APPROVAL_BLOCKING_READINESS = frozenset({READINESS_STALE, READINESS_BLOCKED})


def _source_baselines(db: Session, source_document_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Current (checksum, truth-status) of each source — captured at generation as the grounding
    baseline. Owner-agnostic read (the caller already owns these ids); missing docs are omitted."""
    from app.models.document import Document

    out: dict[str, dict[str, Any]] = {}
    for sid in source_document_ids:
        try:
            doc = db.get(Document, uuid.UUID(str(sid)))
        except (ValueError, TypeError):
            continue
        if doc is not None:
            out[str(sid)] = {
                "checksum": getattr(doc, "checksum", None),
                "status": getattr(doc.active_truth_status, "value", str(doc.active_truth_status)),
            }
    return out


def _classify_source(
    *, present: bool, authorized: bool, status: str | None, recorded_checksum, current_checksum
) -> tuple[str | None, str | None]:
    """Severity of one source for readiness: (None | 'stale' | 'blocked', reason).

    Distinguishes a STATUS change (now historical/superseded/disputed/proposed) from a
    CONTENT/VERSION change (checksum differs from the baseline recorded at grounding) — both
    invalidate the earlier grounding, but for different, separately-reported reasons."""
    from app.rag.trust import NON_ACTIVE_STATUSES

    if not present:
        return "blocked", "missing"
    if not authorized:
        return "blocked", "unauthorized"
    if status in NON_ACTIVE_STATUSES:
        return "stale", f"status:{status}"
    if recorded_checksum is not None and current_checksum is not None and recorded_checksum != current_checksum:
        return "stale", "content_changed"
    return None, None


def _overall_readiness(source_severities: list[str | None], has_open_questions: bool) -> str:
    if any(s == "blocked" for s in source_severities):
        return READINESS_BLOCKED
    if any(s == "stale" for s in source_severities):
        return READINESS_STALE
    if has_open_questions:
        return READINESS_NEEDS_INFORMATION
    return READINESS_READY


def evaluate_blueprint_readiness(
    db: Session, *, owner_id: uuid.UUID, draft: BlueprintDraft
) -> dict[str, Any]:
    """Founder-visible readiness REPORT: classifies a draft READY / NEEDS_INFORMATION / STALE /
    BLOCKED from the CURRENT state of its grounding sources (vs the baseline recorded at
    generation) plus its open questions, with per-source evidence references and reasons.

    READ-ONLY and NON-AUTHORIZING: a READY result grants nothing — approval is a separate,
    server-enforced, Founder-only action (`approve_blueprint_draft`), which re-checks this
    invariant inside its own locked transaction. This report is for display/decision support.
    """
    from app.models.document import Document

    recorded = {
        str(r.get("document_id")): r.get("source_checksum") for r in (draft.source_refs or [])
    }
    sources_report: list[dict[str, Any]] = []
    severities: list[str | None] = []
    for sid in draft.source_document_ids or []:
        sid = str(sid)
        try:
            doc = db.get(Document, uuid.UUID(sid))
        except (ValueError, TypeError):
            doc = None
        present = doc is not None
        authorized = bool(present and doc.uploaded_by == owner_id)
        status = getattr(doc.active_truth_status, "value", str(doc.active_truth_status)) if present else None
        current_checksum = getattr(doc, "checksum", None) if present else None
        severity, reason = _classify_source(
            present=present,
            authorized=authorized,
            status=status,
            recorded_checksum=recorded.get(sid),
            current_checksum=current_checksum,
        )
        severities.append(severity)
        sources_report.append(
            {
                "document_id": sid,
                "severity": severity,
                "reason": reason,
                "current_status": status,
                "content_changed": reason == "content_changed",
                "present": present,
                "authorized": authorized,
            }
        )

    open_questions = list((draft.provenance or {}).get("open_questions") or [])
    readiness = _overall_readiness(severities, bool(open_questions))
    return {
        "draft_id": str(draft.id),
        "title": draft.title,
        "version": draft.version,
        "status": draft.status,
        "readiness": readiness,
        "blocks_approval": readiness in APPROVAL_BLOCKING_READINESS,
        "sources": sources_report,
        "open_questions": open_questions,
        "reasons": [f"{s['document_id']}: {s['reason']}" for s in sources_report if s["reason"]],
    }
