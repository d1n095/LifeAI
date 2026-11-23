"""Round 10 M7: the improvement-proposal service — controlled initiative.

THE BOUNDARY, stated once and enforced structurally:

  1. propose_improvement() is the ONLY thing MainAI's cognitive layers can call. It
     WRITES A ROW and returns. It executes nothing: no code changes, no new agent
     missions, no security-policy touches, no deployment actions. The response the
     router builds says execution_attempted=False explicitly, because the absence of an
     execution path IS the guarantee — the field just makes it visible.
  2. Duplicates collapse: the same (source, normalized title) never spawns endless
     proposals. A rejected proposal CAN be re-proposed if the evidence recurs — the
     recurrence itself is new information.
  3. decide()/mark_implemented()/mark_reverted() are FOUNDER-only transitions (the
     router requires the founder dependency; the service re-checks role). Every decision
     writes an audit_log row: the initiative record is append-only in effect — the
     proposal text is never edited after creation, only decided on.
  4. "Measuring whether accepted improvements work" rides the EXISTING machinery: an
     implemented proposal's outcome is observed through the lesson/guard-observation
     loop already in place; mark_reverted() records the failure in outcome_notes. No
     second measurement system.

No competing memory system is introduced (mission M5 constraint honored here too): the
proposals are an initiative queue, not a knowledge store — knowledge stays in the
claim/evidence/lesson/signal tables that already exist.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.improvement_proposal import ImprovementSource, ImprovementStatus, MainAIImprovementProposal
from app.models.user import User, UserRole


class ImprovementProposalError(ValueError):
    """Domain error — the router translates to the appropriate HTTP status."""


class ForbiddenDecision(ImprovementProposalError):
    """A non-founder tried to decide."""


class InvalidDecision(ImprovementProposalError):
    """An unsupported status transition."""


class AlreadyDecided(ImprovementProposalError):
    """The proposal was already decided; proposals are append-only in effect."""


def _duplicate_key(source: ImprovementSource, title: str) -> str:
    normalized = " ".join((title or "").lower().split())
    return hashlib.sha256(f"{source.value}|{normalized}".encode("utf-8")).hexdigest()[:48]


def propose_improvement(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source: ImprovementSource,
    title: str,
    rationale: str,
    benefit_estimate: str | None = None,
    risk_estimate: str | None = None,
    proposed_by: str = "mainai",
) -> tuple[MainAIImprovementProposal, bool]:
    """Record ONE improvement proposal. Returns (proposal, duplicate). NEVER executes
    anything — see the module docstring. The duplicate check collapses open duplicates."""
    key = _duplicate_key(source, title)
    existing = (
        db.query(MainAIImprovementProposal)
        .filter(
            MainAIImprovementProposal.owner_id == owner_id,
            MainAIImprovementProposal.duplicate_key == key,
            MainAIImprovementProposal.status == ImprovementStatus.proposed,
        )
        .first()
    )
    if existing is not None:
        return existing, True

    proposal = MainAIImprovementProposal(
        owner_id=owner_id, source_type=source, title=title.strip(), rationale=rationale,
        benefit_estimate=benefit_estimate, risk_estimate=risk_estimate, duplicate_key=key,
        status=ImprovementStatus.proposed, proposed_by=proposed_by,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal, False


def _require_founder(db: Session, user: User) -> None:
    if user.role != UserRole.founder:
        raise ForbiddenDecision("improvement-proposal decisions are founder-only")


def decide_proposal(
    db: Session, *, user: User, proposal_id: uuid.UUID, decision: ImprovementStatus, notes: str
) -> MainAIImprovementProposal:
    """Founder-only: accept or reject. The proposal TEXT stays untouched (append-only in
    effect); only the decision fields move, and the decision lands on the audit trail."""
    _require_founder(db, user)
    if decision not in (ImprovementStatus.accepted, ImprovementStatus.rejected):
        raise InvalidDecision("decide_proposal accepts only accepted/rejected")
    proposal = db.query(MainAIImprovementProposal).filter_by(id=proposal_id, owner_id=user.id).one()
    if proposal.status != ImprovementStatus.proposed:
        raise AlreadyDecided(f"proposal already decided: {proposal.status.value}")
    proposal.status = decision
    proposal.decision_notes = notes
    proposal.decided_by = f"founder:{user.email}"
    proposal.decided_at = datetime.utcnow()
    record_audit(db, user_id=user.id, action="improvement_proposal_decision",
                 entity_type="mainai_improvement_proposal", entity_id=str(proposal.id),
                 detail=json.dumps({"decision": decision.value, "notes": notes}))
    db.commit()
    db.refresh(proposal)
    return proposal


def mark_implemented(db: Session, *, user: User, proposal_id: uuid.UUID, notes: str) -> MainAIImprovementProposal:
    """Founder-only: the accepted improvement was implemented (by the founder, or by an
    authorized lane) — record it so effectiveness measurement can start."""
    _require_founder(db, user)
    proposal = db.query(MainAIImprovementProposal).filter_by(id=proposal_id, owner_id=user.id).one()
    if proposal.status != ImprovementStatus.accepted:
        raise InvalidDecision("only an accepted proposal can be marked implemented")
    proposal.status = ImprovementStatus.implemented
    proposal.outcome_notes = notes
    record_audit(db, user_id=user.id, action="improvement_proposal_implemented",
                 entity_type="mainai_improvement_proposal", entity_id=str(proposal.id), detail=notes)
    db.commit()
    db.refresh(proposal)
    return proposal


def mark_reverted(db: Session, *, user: User, proposal_id: uuid.UUID, notes: str) -> MainAIImprovementProposal:
    """Founder-only: the implemented change did NOT work — revert is a first-class,
    recorded outcome, never a hidden rollback."""
    _require_founder(db, user)
    proposal = db.query(MainAIImprovementProposal).filter_by(id=proposal_id, owner_id=user.id).one()
    if proposal.status != ImprovementStatus.implemented:
        raise InvalidDecision("only an implemented proposal can be reverted")
    proposal.status = ImprovementStatus.reverted
    proposal.outcome_notes = notes
    record_audit(db, user_id=user.id, action="improvement_proposal_reverted",
                 entity_type="mainai_improvement_proposal", entity_id=str(proposal.id), detail=notes)
    db.commit()
    db.refresh(proposal)
    return proposal


def list_proposals(db: Session, *, owner_id: uuid.UUID, status: ImprovementStatus | None = None) -> list[MainAIImprovementProposal]:
    query = db.query(MainAIImprovementProposal).filter(MainAIImprovementProposal.owner_id == owner_id)
    if status is not None:
        query = query.filter(MainAIImprovementProposal.status == status)
    return query.order_by(MainAIImprovementProposal.created_at.desc()).limit(100).all()
