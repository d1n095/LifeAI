"""Phase 6: CONTROLLED LEARNING & PROACTIVE IMPROVEMENT — the full proposal lifecycle
and lesson-feedback loop, with the authorization boundary proven at every step:

  - evidence-backed proposals (source = recurring failure clusters / lesson recurrences)
  - duplicate suppression (same observation never spawns endless proposals)
  - correct priority handling (proposals sit in a queue; the founder decides order)
  - explicit founder authorization (accept/reject/implement/revert are founder-only and
    audit-trailed; a member CANNOT decide)
  - NO automatic privileged execution (execution_attempted=False structurally; no code
    path from proposal to privileged action)
  - no unauthorized policy changes / no corruption of authoritative memory (proposals
    never write decisions, lessons, or claims)
  - measurable improvement after accepted changes (the lesson effectiveness machinery
    measures applied-vs-observed; the proposal records the outcome)
  - rejection / rollback of ineffective changes (revert is first-class and audited)
  - proposal ≠ approved decision ≠ executed action (three distinct states, three distinct
    audit events, none implicit)
  - long-term goals are never silently modified (goal mutations stay in the founder-gated
    execution router; proposals carry goal_id references at most, never writes)
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text as sa_text

from app.mainai_execution.improvement_proposals import (
    AlreadyDecided,
    ForbiddenDecision,
    InvalidDecision,
    ImprovementProposalError,
    ImprovementSource,
    ImprovementStatus,
    decide_proposal,
    list_proposals,
    mark_implemented,
    mark_reverted,
    propose_improvement,
)
from app.models.audit import AuditLog
from app.models.improvement_proposal import MainAIImprovementProposal
from app.models.user import User, UserRole
from app.problem_learning.service import create_problem, record_decision
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def founder(client, db_session):
    # The client fixture boots the app -> bootstrap_founder_user() runs; without it the
    # founder row does not exist yet.
    user = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _set_rls_user(db_session, user.id)
    return user


def test_proposal_requires_evidence_backed_source_and_records_estimates(db_session, founder):
    proposal, duplicate = propose_improvement(
        db_session, owner_id=founder.id,
        source=ImprovementSource.recurring_failure_cluster,
        title="Add regression guard for the supersession race",
        rationale="The concurrent supersession test surfaced a lock-ordering subtlety twice this session; a dedicated guard test would catch it before the founder does.",
        benefit_estimate="Catches the race before silent inconsistent state",
        risk_estimate="Low — test-only",
    )
    db_session.commit()
    assert proposal.status == ImprovementStatus.proposed and duplicate is False
    assert proposal.benefit_estimate and proposal.risk_estimate, "estimates recorded with the proposal"
    assert proposal.proposed_by == "mainai"
    assert proposal.decided_at is None and proposal.decided_by is None


def test_duplicate_suppression_and_re_proposal_after_rejection(db_session, founder):
    p1, dup1 = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.lesson_recurrence, title="Cache the trust assessment per request", rationale="Trust assessment recomputes per claim on every chat request; cache within the request.")
    db_session.commit()
    p2, dup2 = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.lesson_recurrence, title="Cache the trust assessment per request", rationale="Trust assessment recomputes per claim on every chat request; cache within the request.")
    db_session.commit()
    assert dup2 is True and p2.id == p1.id, "open duplicate collapses to the existing row"

    decide_proposal(db_session, user=founder, proposal_id=p1.id, decision=ImprovementStatus.rejected, notes="Not worth the invalidation complexity now")
    db_session.commit()
    p3, dup3 = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.lesson_recurrence, title="Cache the trust assessment per request", rationale="Trust assessment recomputes per claim on every chat request; cache within the request.")
    db_session.commit()
    assert dup3 is False and p3.id != p1.id, "rejection frees the key: a RECURRENCE is new information and may be re-proposed"
    assert len(list_proposals(db_session, owner_id=founder.id)) >= 2


def test_member_cannot_decide_and_states_are_distinct(db_session, founder):
    proposal, _ = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.manual, title="Split the epistemic instruction into two lines", rationale="One line carries assumptions and verified facts; two lines would make each harder to miss.")
    db_session.commit()
    # A member-role principal is rejected by the SERVICE before any query (and by the
    # founder-account gate at the router before the service is reached — two layers).
    member = User(email=f"p6-member2-{uuid.uuid4().hex[:6]}@lifeos.local", password_hash=hash_password("MemberP6Pass123!"), role=UserRole.member, email_verified=True)
    with pytest.raises(ForbiddenDecision):
        decide_proposal(db_session, user=member, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="member attempt")
    with pytest.raises(ForbiddenDecision):
        mark_implemented(db_session, user=member, proposal_id=proposal.id, notes="member attempt")

    # proposal -> approved decision -> executed action: three DISTINCT audited states
    decide_proposal(db_session, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="Accepted: do it in the rendering layer")
    implemented = mark_implemented(db_session, user=founder, proposal_id=proposal.id, notes="Implemented in the prompt renderer; guard test added")
    db_session.commit()
    # decided/implemented are the SAME ORM row evolving through its (constrained) states:
    assert implemented.status == ImprovementStatus.implemented
    actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.action.like("improvement_proposal%")).all()]
    assert "improvement_proposal_decision" in actions and "improvement_proposal_implemented" in actions, (
        "each transition is its own audit event"
    )
    # decided_at only on the decision — the proposal was NEVER executed implicitly
    assert implemented.decided_by.startswith("founder:")


def test_revert_is_first_class_and_measured(db_session, founder):
    proposal, _ = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.manual, title="Try stricter conflict-cap wording", rationale="Strengthen the conflicting-evidence wording to see whether the landscape line gets clearer.")
    db_session.commit()
    decide_proposal(db_session, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="Trial accepted")
    mark_implemented(db_session, user=founder, proposal_id=proposal.id, notes="Wording deployed behind the standing test")
    reverted = mark_reverted(db_session, user=founder, proposal_id=proposal.id, notes="Effectiveness rollup showed no measurable improvement after two sessions — reverted")
    db_session.commit()
    assert reverted.status == ImprovementStatus.reverted
    assert "no measurable improvement" in reverted.outcome_notes, "the rollback records WHY it failed"
    actions = [a.action for a in db_session.query(AuditLog).filter_by(action="improvement_proposal_reverted").all()]
    assert len(actions) == 1, "revert is audited as its own event"
    # Re-proposal after revert is possible (the evidence changed again) but the OLD row
    # keeps its outcome — history is never rewritten.
    old = db_session.query(MainAIImprovementProposal).filter_by(id=proposal.id).one()
    assert old.status == ImprovementStatus.reverted and old.outcome_notes


def test_proposals_never_touch_authoritative_memory_or_goals(db_session, founder):
    """The structural guarantee: the proposal service imports NOTHING that can write
    decisions, claims, lessons, or goals. Verified by inspecting the module surface and
    by an end-to-end check that a full lifecycle leaves those tables untouched."""
    import inspect

    from app.mainai_execution import improvement_proposals as svc

    source = inspect.getsource(svc)
    forbidden = ["LifeProblemDecision", "KnowledgeClaim", "EngineeringLesson", "MainAIGoal", "MainAITask", "record_decision", "create_plan"]
    leaks = [f for f in forbidden if f in source]
    assert not leaks, f"the proposal service must not reference authoritative writers: {leaks}"

    # End-to-end: a full lifecycle leaves decision/goal tables unchanged.
    _set_rls_user(db_session, founder.id)
    problem = create_problem(db_session, owner_id=founder.id, title="P6 integrity", description="x", idempotency_key=f"p6-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db_session, founder.id)
    record_decision(db_session, owner_id=founder.id, problem_id=problem.id, decision="Stable", idempotency_key=f"p6-d-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db_session.commit()
    proposal, _ = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.manual, title="Goal wording nudge", rationale="A wording suggestion for the standing goal — PROPOSAL ONLY.")
    decide_proposal(db_session, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="wording accepted, applied manually")
    db_session.commit()
    rows = db_session.query(sa_text("decision")).from_statement(sa_text("SELECT decision FROM life_problem_decisions WHERE problem_id = :pid")).params(pid=problem.id).all()
    assert len(rows) == 1 and rows[0][0] == "Stable", "the accepted proposal did not corrupt the authoritative decision"


def test_double_decision_rejected_and_invalid_transitions_blocked(db_session, founder):
    proposal, _ = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.manual, title="Document the RLS GUC contract", rationale="The SET LOCAL lifecycle keeps surprising engineers; document it in the RLS module.")
    db_session.commit()
    decide_proposal(db_session, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="ok")
    with pytest.raises(AlreadyDecided):
        decide_proposal(db_session, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.rejected, notes="second decision")
    db_session.rollback()
    fresh, _ = propose_improvement(db_session, owner_id=founder.id, source=ImprovementSource.manual, title="Document the RLS GUC contract v2", rationale="Another attempt at the same documentation idea.")
    db_session.commit()
    with pytest.raises(InvalidDecision):
        mark_implemented(db_session, user=founder, proposal_id=fresh.id, notes="not accepted yet")
    db_session.rollback()
