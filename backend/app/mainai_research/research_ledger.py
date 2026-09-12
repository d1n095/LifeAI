"""Research Ledger -- durable, auditable investigation/hypothesis/evidence/confidence-history
store (migration 0073). See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for
the architecture decision.

CONFIDENCE CHANGE MUST HAVE A REASON: `update_hypothesis_confidence()` requires a non-empty
`reason` and ALWAYS appends a `mainai_research_confidence_history` row (append-only, DB-enforced
via the existing `intelligence_governance_deny_mutation()` trigger) -- it never merely
overwrites `current_confidence` without a durable trail.

REJECTED EVIDENCE != DELETED EVIDENCE: `reopen_evidence()` never deletes or silently mutates the
original row's own `lifecycle_status` history -- it records a `mainai_research_reopen_events`
row (append-only) and only then updates the CURRENT `lifecycle_status`, preserving the original
rationale in that same, still-queryable evidence row.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.mainai_research.types import (
    EvidenceLifecycleStatus,
    EvidenceRole,
    EvidenceState,
    HypothesisStatus,
    InvestigationStatus,
    ResearchError,
)


def _as_float(value) -> float | None:
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def record_investigation(db: Session, *, owner_id: uuid.UUID, question: str, idempotency_key: str) -> dict:
    if not question.strip():
        raise ResearchError("question must not be empty")
    existing = db.execute(
        text("SELECT * FROM mainai_research_investigations WHERE owner_id=:o AND idempotency_key=:k"),
        {"o": owner_id, "k": idempotency_key},
    ).mappings().first()
    if existing:
        if existing["question"] != question:
            raise ResearchError("idempotency key reused with a different question")
        return dict(existing)
    row_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO mainai_research_investigations(id, owner_id, question, idempotency_key) "
            "VALUES (:id, :o, :q, :k)"
        ),
        {"id": row_id, "o": owner_id, "q": question, "k": idempotency_key},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_investigations WHERE id=:id"), {"id": row_id}).mappings().one())


def mark_investigation_saturated(db: Session, *, owner_id: uuid.UUID, investigation_id: uuid.UUID, reason: str) -> dict:
    """SATURATED_FOR_NOW != PERMANENTLY CLOSED -- distinct from `close_investigation()` below;
    a saturated investigation still carries its own reopening triggers (see
    `epistemic_caution.py`/`falsification.py` for what those are)."""

    if not reason.strip():
        raise ResearchError("saturation requires a non-empty reason")
    row = db.execute(
        text("SELECT * FROM mainai_research_investigations WHERE id=:id AND owner_id=:o FOR UPDATE"),
        {"id": investigation_id, "o": owner_id},
    ).mappings().first()
    if row is None:
        raise ResearchError("investigation is missing or belongs to another owner")
    db.execute(
        text(
            "UPDATE mainai_research_investigations SET status=:s, saturation_reason=:r, updated_at=clock_timestamp() "
            "WHERE id=:id AND owner_id=:o"
        ),
        {"s": InvestigationStatus.SATURATED_FOR_NOW.value, "r": reason, "id": investigation_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_investigations WHERE id=:id"), {"id": investigation_id}).mappings().one())


def reopen_investigation(db: Session, *, owner_id: uuid.UUID, investigation_id: uuid.UUID) -> dict:
    """A SATURATED_FOR_NOW investigation can always return to ACTIVE -- a CLOSED one cannot
    (mirrors this codebase's own TerminalStateError doctrine for genuinely terminal states)."""

    row = db.execute(
        text("SELECT * FROM mainai_research_investigations WHERE id=:id AND owner_id=:o FOR UPDATE"),
        {"id": investigation_id, "o": owner_id},
    ).mappings().first()
    if row is None:
        raise ResearchError("investigation is missing or belongs to another owner")
    if row["status"] == InvestigationStatus.CLOSED.value:
        raise ResearchError("a closed investigation cannot be reopened through this function")
    db.execute(
        text("UPDATE mainai_research_investigations SET status=:s, saturation_reason=NULL, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"s": InvestigationStatus.ACTIVE.value, "id": investigation_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_investigations WHERE id=:id"), {"id": investigation_id}).mappings().one())


def record_hypothesis(db: Session, *, owner_id: uuid.UUID, investigation_id: uuid.UUID, statement: str, idempotency_key: str) -> dict:
    if not statement.strip():
        raise ResearchError("statement must not be empty")
    investigation = db.execute(
        text("SELECT id FROM mainai_research_investigations WHERE id=:id AND owner_id=:o"),
        {"id": investigation_id, "o": owner_id},
    ).first()
    if investigation is None:
        raise ResearchError("investigation_id does not belong to owner_id")
    existing = db.execute(
        text("SELECT * FROM mainai_research_hypotheses WHERE owner_id=:o AND idempotency_key=:k"),
        {"o": owner_id, "k": idempotency_key},
    ).mappings().first()
    if existing:
        if existing["statement"] != statement or existing["investigation_id"] != investigation_id:
            raise ResearchError("idempotency key reused with different fields")
        return dict(existing)
    row_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO mainai_research_hypotheses(id, owner_id, investigation_id, statement, idempotency_key) "
            "VALUES (:id, :o, :inv, :s, :k)"
        ),
        {"id": row_id, "o": owner_id, "inv": investigation_id, "s": statement, "k": idempotency_key},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_hypotheses WHERE id=:id"), {"id": row_id}).mappings().one())


def record_evidence(
    db: Session,
    *,
    owner_id: uuid.UUID,
    hypothesis_id: uuid.UUID,
    role: EvidenceRole,
    underlying_source_id: str,
    evidence_state: EvidenceState,
    summary: str,
    idempotency_key: str,
    lifecycle_status: EvidenceLifecycleStatus = EvidenceLifecycleStatus.UNRESOLVED,
    provenance: dict | None = None,
) -> dict:
    if not summary.strip():
        raise ResearchError("summary must not be empty")
    hyp = db.execute(text("SELECT id FROM mainai_research_hypotheses WHERE id=:id AND owner_id=:o"), {"id": hypothesis_id, "o": owner_id}).first()
    if hyp is None:
        raise ResearchError("hypothesis_id does not belong to owner_id")
    existing = db.execute(
        text("SELECT * FROM mainai_research_evidence_links WHERE owner_id=:o AND idempotency_key=:k"),
        {"o": owner_id, "k": idempotency_key},
    ).mappings().first()
    if existing:
        return dict(existing)
    row_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO mainai_research_evidence_links"
            "(id, owner_id, hypothesis_id, role, lifecycle_status, underlying_source_id, evidence_state, summary, provenance, idempotency_key) "
            "VALUES (:id, :o, :hyp, :role, :lifecycle, :source, :state, :summary, CAST(:prov AS jsonb), :k)"
        ),
        {
            "id": row_id, "o": owner_id, "hyp": hypothesis_id, "role": role.value, "lifecycle": lifecycle_status.value,
            "source": underlying_source_id, "state": evidence_state.value, "summary": summary,
            "prov": json.dumps(provenance or {}), "k": idempotency_key,
        },
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_evidence_links WHERE id=:id"), {"id": row_id}).mappings().one())


def reject_evidence(db: Session, *, owner_id: uuid.UUID, evidence_id: uuid.UUID, rationale: str, new_status: EvidenceLifecycleStatus = EvidenceLifecycleStatus.REJECTED_AS_SUPPORT) -> dict:
    """REJECTED_AS_SUPPORT != PROVEN_FALSE: the caller must explicitly choose which status
    applies -- this function never silently defaults a rejection to PROVEN_FALSE."""

    if new_status not in (EvidenceLifecycleStatus.REJECTED_AS_SUPPORT, EvidenceLifecycleStatus.PROVEN_FALSE, EvidenceLifecycleStatus.INSUFFICIENT_EVIDENCE, EvidenceLifecycleStatus.DEPRIORITIZED):
        raise ResearchError(f"{new_status} is not a valid rejection outcome")
    if not rationale.strip():
        raise ResearchError("rejection requires a non-empty rationale")
    row = db.execute(text("SELECT * FROM mainai_research_evidence_links WHERE id=:id AND owner_id=:o FOR UPDATE"), {"id": evidence_id, "o": owner_id}).mappings().first()
    if row is None:
        raise ResearchError("evidence_id is missing or belongs to another owner")
    db.execute(
        text("UPDATE mainai_research_evidence_links SET lifecycle_status=:s, rejection_rationale=:r, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"s": new_status.value, "r": rationale, "id": evidence_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_evidence_links WHERE id=:id"), {"id": evidence_id}).mappings().one())


def reopen_evidence(db: Session, *, owner_id: uuid.UUID, evidence_id: uuid.UUID, reason: str, new_lifecycle_status: EvidenceLifecycleStatus) -> dict:
    """REOPEN -> REINVESTIGATE BOTH SIDES: this function only records the reopen event and the
    new lifecycle status -- it never re-evaluates the hypothesis's confidence itself (a real
    caller must separately call `update_hypothesis_confidence()` with its own, independently
    re-investigated reasoning; see this module's own docstring: NEW SUPPORT DOES NOT
    AUTOMATICALLY VALIDATE OLD EVIDENCE)."""

    if not reason.strip():
        raise ResearchError("reopening requires a non-empty reason")
    row = db.execute(text("SELECT * FROM mainai_research_evidence_links WHERE id=:id AND owner_id=:o FOR UPDATE"), {"id": evidence_id, "o": owner_id}).mappings().first()
    if row is None:
        raise ResearchError("evidence_id is missing or belongs to another owner")
    original_status = row["lifecycle_status"]
    db.execute(
        text(
            "INSERT INTO mainai_research_reopen_events(owner_id, evidence_link_id, reason, original_lifecycle_status, new_lifecycle_status) "
            "VALUES (:o, :eid, :reason, :orig, :new)"
        ),
        {"o": owner_id, "eid": evidence_id, "reason": reason, "orig": original_status, "new": new_lifecycle_status.value},
    )
    db.execute(
        text("UPDATE mainai_research_evidence_links SET lifecycle_status=:s, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"s": new_lifecycle_status.value, "id": evidence_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_evidence_links WHERE id=:id"), {"id": evidence_id}).mappings().one())


def update_hypothesis_confidence(
    db: Session, *, owner_id: uuid.UUID, hypothesis_id: uuid.UUID, new_confidence: float, reason: str, evidence_link_id: uuid.UUID | None = None,
) -> dict:
    """CONFIDENCE CHANGE MUST HAVE A REASON -- structurally required, not just convention."""

    if not reason.strip():
        raise ResearchError("a confidence change requires a non-empty reason")
    if not 0.0 <= new_confidence <= 1.0:
        raise ResearchError("new_confidence must be within 0.0..1.0")
    row = db.execute(text("SELECT * FROM mainai_research_hypotheses WHERE id=:id AND owner_id=:o FOR UPDATE"), {"id": hypothesis_id, "o": owner_id}).mappings().first()
    if row is None:
        raise ResearchError("hypothesis_id is missing or belongs to another owner")
    previous = _as_float(row["current_confidence"])
    db.execute(
        text(
            "INSERT INTO mainai_research_confidence_history(owner_id, hypothesis_id, previous_confidence, new_confidence, reason, evidence_link_id) "
            "VALUES (:o, :hid, :prev, :new, :reason, :eid)"
        ),
        {"o": owner_id, "hid": hypothesis_id, "prev": previous, "new": new_confidence, "reason": reason, "eid": evidence_link_id},
    )
    db.execute(
        text("UPDATE mainai_research_hypotheses SET current_confidence=:c, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"c": new_confidence, "id": hypothesis_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_hypotheses WHERE id=:id"), {"id": hypothesis_id}).mappings().one())


def set_hypothesis_status(db: Session, *, owner_id: uuid.UUID, hypothesis_id: uuid.UUID, status: HypothesisStatus, increment_falsification_round: bool = False) -> dict:
    row = db.execute(text("SELECT * FROM mainai_research_hypotheses WHERE id=:id AND owner_id=:o FOR UPDATE"), {"id": hypothesis_id, "o": owner_id}).mappings().first()
    if row is None:
        raise ResearchError("hypothesis_id is missing or belongs to another owner")
    db.execute(
        text(
            "UPDATE mainai_research_hypotheses SET status=:s, updated_at=clock_timestamp(), "
            "falsification_rounds = falsification_rounds + CASE WHEN :inc THEN 1 ELSE 0 END "
            "WHERE id=:id AND owner_id=:o"
        ),
        {"s": status.value, "inc": increment_falsification_round, "id": hypothesis_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_research_hypotheses WHERE id=:id"), {"id": hypothesis_id}).mappings().one())


def list_confidence_history(db: Session, *, owner_id: uuid.UUID, hypothesis_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM mainai_research_confidence_history WHERE owner_id=:o AND hypothesis_id=:h ORDER BY recorded_at"),
        {"o": owner_id, "h": hypothesis_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def list_reopen_events(db: Session, *, owner_id: uuid.UUID, evidence_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM mainai_research_reopen_events WHERE owner_id=:o AND evidence_link_id=:e ORDER BY recorded_at"),
        {"o": owner_id, "e": evidence_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def list_evidence_for_hypothesis(db: Session, *, owner_id: uuid.UUID, hypothesis_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM mainai_research_evidence_links WHERE owner_id=:o AND hypothesis_id=:h ORDER BY created_at"),
        {"o": owner_id, "h": hypothesis_id},
    ).mappings().all()
    return [dict(r) for r in rows]
