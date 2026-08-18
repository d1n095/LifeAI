"""Life Causal Diagnosis Interface -- the deterministic boundary between "something was
observed," "a cause is suspected," and "a cause is actually proven." See migration 0050's own
module docstring for the full reuse map.

This module NEVER infers `hypothesis_category`, `authority`, or `basis` -- every call site
supplies them explicitly, the same doctrine `app.capability_reality.service`/`app.founder_
memory.service` already establish for their own facts. There is no code path here that reads a
test failure or an error message and decides for itself that it was a code regression versus
an environment issue -- that classification is always the caller's own explicit assertion.

`record_diagnosis()` ALWAYS inserts a new row -- `observation` is never rewritten on an
existing row. `prove_diagnosis_cause()`/`rule_out_diagnosis()` transition the SAME row's own
`epistemic_stage` in place (they do not create a new row) -- unlike a full correction
(`supersedes_diagnosis_id`, a genuinely new diagnosis correcting an earlier one), moving from
`hypothesis` to `proven_cause`/`ruled_out` is the SAME diagnosis reaching a more certain state
about the SAME observation, not a different fact. `prove_diagnosis_cause()` cannot be called
without a real evidence reference -- the database's own CHECK constraint refuses the row even
if a caller tried to bypass this function and write the transition directly."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.diagnosis import DiagnosisRecord


class DiagnosisError(ValueError):
    pass


def record_diagnosis(
    db: Session,
    *,
    owner_id: uuid.UUID,
    observation: str,
    idempotency_key: str,
    hypothesis_category: str = "unknown",
    hypothesis_reasoning: str | None = None,
    epistemic_stage: str = "observed",
    confidence: float | None = None,
    authority: str = "unknown",
    basis: str = "unknown",
    proven_evidence_id: uuid.UUID | None = None,
    supersedes_diagnosis_id: uuid.UUID | None = None,
    provenance: dict[str, Any] | None = None,
) -> DiagnosisRecord:
    """Records ONE causal diagnosis. `epistemic_stage="proven_cause"` requires
    `proven_evidence_id` to already be supplied in the SAME call -- the database's own CHECK
    constraint fails closed if it is not. When `supersedes_diagnosis_id` is supplied, the
    referenced diagnosis (which MUST already belong to the same owner, row-locked to close a
    concurrent-supersede race) is left entirely untouched by this call -- superseding a
    diagnosis is a fact about the NEW row, never a mutation of the old one; a caller that wants
    the old diagnosis explicitly marked `ruled_out` calls `rule_out_diagnosis()` separately."""

    if supersedes_diagnosis_id is not None:
        old = db.execute(
            select(DiagnosisRecord).where(DiagnosisRecord.id == supersedes_diagnosis_id, DiagnosisRecord.owner_id == owner_id).with_for_update()
        ).scalar_one_or_none()
        if old is None:
            raise DiagnosisError("superseded diagnosis is missing or belongs to another owner")

    existing = db.execute(
        select(DiagnosisRecord).where(DiagnosisRecord.owner_id == owner_id, DiagnosisRecord.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    values: dict[str, Any] = dict(
        observation=observation, hypothesis_category=hypothesis_category, hypothesis_reasoning=hypothesis_reasoning,
        epistemic_stage=epistemic_stage, confidence=confidence, authority=authority, basis=basis,
        proven_evidence_id=proven_evidence_id, supersedes_diagnosis_id=supersedes_diagnosis_id, provenance=provenance or {},
    )
    if existing:
        differing = [key for key, value in values.items() if getattr(existing, key) != value]
        if differing:
            raise DiagnosisError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
        return existing

    row = DiagnosisRecord(owner_id=owner_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def prove_diagnosis_cause(db: Session, *, owner_id: uuid.UUID, diagnosis_id: uuid.UUID, evidence_id: uuid.UUID, confidence: float | None = None) -> DiagnosisRecord:
    """The ONLY path that transitions a diagnosis to `proven_cause` -- always requires a real
    evidence reference, mirrored by the DB's own CHECK constraint as a second, independent
    enforcement layer (a caller bypassing this function entirely and writing the row directly
    still cannot reach `proven_cause` without `proven_evidence_id` set)."""

    row = db.execute(select(DiagnosisRecord).where(DiagnosisRecord.id == diagnosis_id, DiagnosisRecord.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise DiagnosisError("diagnosis is missing or belongs to another owner")
    row.epistemic_stage = "proven_cause"
    row.proven_evidence_id = evidence_id
    if confidence is not None:
        row.confidence = confidence
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def rule_out_diagnosis(db: Session, *, owner_id: uuid.UUID, diagnosis_id: uuid.UUID) -> DiagnosisRecord:
    """A hypothesis that further evidence contradicted -- never deleted, kept as durable
    evidence that this specific cause was considered and rejected (so a future diagnosis pass
    does not re-propose the same ruled-out hypothesis without new evidence)."""

    row = db.execute(select(DiagnosisRecord).where(DiagnosisRecord.id == diagnosis_id, DiagnosisRecord.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise DiagnosisError("diagnosis is missing or belongs to another owner")
    row.epistemic_stage = "ruled_out"
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def get_diagnosis(db: Session, *, owner_id: uuid.UUID, diagnosis_id: uuid.UUID) -> DiagnosisRecord | None:
    return db.execute(select(DiagnosisRecord).where(DiagnosisRecord.id == diagnosis_id, DiagnosisRecord.owner_id == owner_id)).scalar_one_or_none()


def list_diagnoses(
    db: Session, *, owner_id: uuid.UUID, hypothesis_category: str | None = None, epistemic_stage: str | None = None
) -> list[DiagnosisRecord]:
    stmt = select(DiagnosisRecord).where(DiagnosisRecord.owner_id == owner_id)
    if hypothesis_category is not None:
        stmt = stmt.where(DiagnosisRecord.hypothesis_category == hypothesis_category)
    if epistemic_stage is not None:
        stmt = stmt.where(DiagnosisRecord.epistemic_stage == epistemic_stage)
    return list(db.execute(stmt.order_by(DiagnosisRecord.observed_at)).scalars().all())


def list_unresolved_diagnoses(db: Session, *, owner_id: uuid.UUID) -> list[DiagnosisRecord]:
    """Everything NOT yet proven or ruled out -- `observed`/`hypothesis` -- the complement of
    "we know why this happened.\""""

    stmt = select(DiagnosisRecord).where(DiagnosisRecord.owner_id == owner_id, DiagnosisRecord.epistemic_stage.in_(("observed", "hypothesis")))
    return list(db.execute(stmt.order_by(DiagnosisRecord.observed_at)).scalars().all())
