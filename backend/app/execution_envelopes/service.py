"""Life Execution Authorization Envelope -- the staging layer between a proposed execution
scope (MainAI's own suggestion) and real, founder-granted execution authority for a
MainAIGoal. See migration 0057's own module docstring for the full architecture.

Hard rule, structural not just documented: `propose_execution_scope()` NEVER writes to
`execution_authorization_envelopes`, directly or indirectly. The ONLY function in this module
that can create an `ExecutionAuthorizationEnvelope` is `authorize_execution_scope()`, and it
ALWAYS requires the caller to supply `authorized_by`/`authorized_paths`/
`authorized_capabilities`/`authorized_risk` explicitly -- the proposal's own suggested values
are never silently copied in. A `proposed_risk="low"` proposal does not imply
`authorized_risk="low"`; the founder's own decision, passed explicitly, is what actually
grants it -- the founder may accept the proposal as-is (by passing the same values back),
narrow it, or explicitly expand it. This module only decides WHETHER to call
`authorize_execution_scope()` and WITH WHAT ARGUMENTS -- WHETHER THE CALLER IS ALLOWED TO
remains entirely the caller's own responsibility (its router, gated by
`Depends(require_founder)`), exactly like `app.work_candidates.service.
authorize_work_candidate()` already established for the layer directly below this one."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.execution_envelope import ExecutionAuthorizationEnvelope, ExecutionScopeProposal
from app.models.mainai_execution import MainAIGoal


class ExecutionEnvelopeError(ValueError):
    pass


def _same(row: ExecutionScopeProposal, values: dict[str, Any]) -> ExecutionScopeProposal:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise ExecutionEnvelopeError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def propose_execution_scope(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    idempotency_key: str,
    repository_identity: str | None = None,
    proposed_paths: list[str] | None = None,
    proposed_capabilities: list[str] | None = None,
    proposed_risk: str = "low",
    proposal_reasoning: str | None = None,
    proposal_strategy: str = "unknown",
    provenance: dict[str, Any] | None = None,
) -> ExecutionScopeProposal:
    """Records ONE execution scope proposal -- never a claim that this scope is authorized,
    only a claim that MainAI suggests a goal might need it. `proposed_paths` deliberately
    defaults to an empty list, never a guessed path: this module has no reliable signal for
    WHICH files a goal should touch (see migration 0057's own module docstring on why a
    task_type-derived guess was explicitly rejected) -- an honest empty proposal, for the
    founder to fill in or for later planning to request explicitly (see the "required_scope -
    authorized_scope != empty -> blocked" doctrine this foundation exists to eventually
    support), beats a fabricated one that only looks informative.

    Fails closed BEFORE any write if `goal_id` does not structurally belong to `owner_id` -- a
    clear, typed error here, with the database's own composite FK (migration 0057) as the
    final backstop."""

    goal = db.execute(select(MainAIGoal).where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)).scalar_one_or_none()
    if goal is None:
        raise ExecutionEnvelopeError(f"goal_id={goal_id} does not belong to owner_id={owner_id}")

    values: dict[str, Any] = dict(
        goal_id=goal_id, repository_identity=repository_identity, proposed_paths=proposed_paths or [],
        proposed_capabilities=proposed_capabilities or [], proposed_risk=proposed_risk,
        proposal_reasoning=proposal_reasoning, proposal_strategy=proposal_strategy, provenance=provenance or {},
    )
    existing = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.owner_id == owner_id, ExecutionScopeProposal.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    row = ExecutionScopeProposal(owner_id=owner_id, idempotency_key=idempotency_key, status="unreviewed", **values)
    db.add(row)
    db.flush()
    return row


def reject_execution_scope(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID, reason: str) -> ExecutionScopeProposal:
    """An explicit "this proposed scope is not right, and no envelope should be authorized
    from it" outcome -- never deletes the row, so the same rejected proposal is not
    re-surfaced for review indefinitely without a durable record that it was already
    considered."""

    row = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ExecutionEnvelopeError("execution scope proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ExecutionEnvelopeError(f"execution scope proposal is already {row.status}, not unreviewed")
    row.status = "rejected"
    row.rejected_reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def authorize_execution_scope(
    db: Session,
    *,
    owner_id: uuid.UUID,
    proposal_id: uuid.UUID,
    authorized_by: str,
    authorized_paths: list[str],
    authorized_capabilities: list[str],
    authorized_risk: str,
    envelope_idempotency_key: str,
) -> tuple[ExecutionScopeProposal, ExecutionAuthorizationEnvelope]:
    """The ONLY path from a proposed execution scope to real, founder-granted execution
    authority -- PROPOSED_SCOPE != AUTHORIZED_SCOPE enforced here, not just in the schema's
    own CHECK constraints. `authorized_by`/`authorized_paths`/`authorized_capabilities`/
    `authorized_risk` are ALWAYS the caller's own explicit assertion (this function has no
    default that reads them off the proposal itself) -- accepting the proposal as-is means the
    caller passes the SAME `proposed_paths`/`proposed_capabilities`/`proposed_risk` back
    explicitly, narrowing or expanding means passing different ones; either way it is a
    deliberate, reviewed act of granting authority, never an automatic copy.

    If this goal already has a current (`status='active'`) envelope, it is superseded (never
    mutated) by the new one -- the founder's original authorization decision remains durably
    auditable even after a later re-authorization changes it."""

    row = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ExecutionEnvelopeError("execution scope proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ExecutionEnvelopeError(f"execution scope proposal is already {row.status}, not unreviewed")

    prior_envelope = db.execute(
        select(ExecutionAuthorizationEnvelope).where(
            ExecutionAuthorizationEnvelope.owner_id == owner_id,
            ExecutionAuthorizationEnvelope.goal_id == row.goal_id,
            ExecutionAuthorizationEnvelope.status == "active",
        ).with_for_update()
    ).scalar_one_or_none()

    envelope = ExecutionAuthorizationEnvelope(
        owner_id=owner_id, goal_id=row.goal_id, source_proposal_id=row.id,
        repository_identity=row.repository_identity, authorized_paths=authorized_paths,
        authorized_capabilities=authorized_capabilities, authorized_risk=authorized_risk,
        authorized_by=authorized_by, idempotency_key=envelope_idempotency_key,
        supersedes_envelope_id=prior_envelope.id if prior_envelope is not None else None,
        provenance={"authorized_from_proposal_id": str(row.id)},
    )
    db.add(envelope)
    db.flush()

    if prior_envelope is not None:
        prior_envelope.status = "superseded"
        db.flush()

    row.status = "authorized"
    row.authorized_envelope_id = envelope.id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row, envelope


def get_execution_scope_proposal(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID) -> ExecutionScopeProposal | None:
    return db.execute(select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id)).scalar_one_or_none()


def list_execution_scope_proposals(db: Session, *, owner_id: uuid.UUID, status: str | None = None, goal_id: uuid.UUID | None = None) -> list[ExecutionScopeProposal]:
    stmt = select(ExecutionScopeProposal).where(ExecutionScopeProposal.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(ExecutionScopeProposal.status == status)
    if goal_id is not None:
        stmt = stmt.where(ExecutionScopeProposal.goal_id == goal_id)
    return list(db.execute(stmt.order_by(ExecutionScopeProposal.observed_at)).scalars().all())


def list_unreviewed_execution_scope_proposals(db: Session, *, owner_id: uuid.UUID) -> list[ExecutionScopeProposal]:
    return list_execution_scope_proposals(db, owner_id=owner_id, status="unreviewed")


def get_execution_authorization_envelope(db: Session, *, owner_id: uuid.UUID, envelope_id: uuid.UUID) -> ExecutionAuthorizationEnvelope | None:
    return db.execute(select(ExecutionAuthorizationEnvelope).where(ExecutionAuthorizationEnvelope.id == envelope_id, ExecutionAuthorizationEnvelope.owner_id == owner_id)).scalar_one_or_none()


def get_current_execution_envelope(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> ExecutionAuthorizationEnvelope | None:
    """The safe-by-default "what is this goal currently authorized to touch, if anything"
    query -- the same role `list_current_project_entities()`/`list_current_diagnoses()` play
    for their own sibling foundations. Returns None (not the most recent row regardless of
    status) if the goal has never been authorized, or if its only envelope was superseded
    without a replacement -- callers must treat "no current envelope" as "not eligible for
    Supervisor execution", never fall back to a stale one."""

    return db.execute(
        select(ExecutionAuthorizationEnvelope).where(
            ExecutionAuthorizationEnvelope.owner_id == owner_id,
            ExecutionAuthorizationEnvelope.goal_id == goal_id,
            ExecutionAuthorizationEnvelope.status == "active",
        )
    ).scalar_one_or_none()


def list_execution_authorization_envelopes(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> list[ExecutionAuthorizationEnvelope]:
    """Full history for a goal, oldest first -- including superseded envelopes, for audit."""

    return list(
        db.execute(
            select(ExecutionAuthorizationEnvelope)
            .where(ExecutionAuthorizationEnvelope.owner_id == owner_id, ExecutionAuthorizationEnvelope.goal_id == goal_id)
            .order_by(ExecutionAuthorizationEnvelope.authorized_at)
        ).scalars().all()
    )
