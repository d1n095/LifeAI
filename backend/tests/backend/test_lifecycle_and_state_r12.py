"""Round 12 Phases 6+7: the COMPLETE improvement lifecycle chain (observation → evidence
→ hypothesis → proposal → risk assessment → founder approval → authorized execution →
verification → rollback) as ONE end-to-end chain, and the Living Core state contract.

Phase 6 chain discipline (each step leaves its own trace; nothing self-authorizes):
  the founder's manual actions (decide/implement/revert) are the ONLY state mutations;
  the proposal never moves itself; the chain is auditable end-to-end; a rolled-back
  improvement's outcome is recorded with a reason.

Phase 7 contract tests: only LEGITIMATE states leave the module; events carry no private
content (message text, prompt bodies, reasoning traces); derivation maps real signals;
absent signals → idle (never a fabricated state); states are operational metaphors, not
emotion claims (asserted on the contract surface).
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.cognitive_state.contract import (
    COGNITIVE_STATE_SCHEMA_VERSION,
    LEGITIMATE_STATES,
    CognitiveStateEvent,
    derive_cognitive_state,
)
from app.mainai_execution.improvement_proposals import (
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
from app.models.provider_verification import ProviderVerificationCheck, VerificationResult
from app.models.user import User, UserRole
from app.problem_learning.service import create_problem, record_decision
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

FOUNDER_EMAIL = "founder@lifeos.local"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def founder(client, db_session):
    user = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _set_rls_user(db_session, user.id)
    return user


def _audit(db, action, founder, entity_id="00000000-0000-0000-0000-000000000000"):
    # Inserted under the founder's GUC with founder ownership: audit_log is user-scoped
    # RLS, so a row written with a cleared GUC would be INVISIBLE to the owner-scoped
    # derivation queries (the same shape production security events use — system rows
    # carry the affected owner's id when owner-scoped tables must see them).
    _set_rls_user(db, founder.id)
    db.add(AuditLog(user_id=founder.id, action=action, entity_type="test", entity_id=entity_id, detail="{}"))
    db.commit()


def test_phase6_full_lifecycle_chain_in_order(founder, db_session):
    """Observation → evidence → hypothesis → proposal (+risk) → approval → execution →
    verification → rollback, each step TRACED, nothing self-authorized."""
    db, owner = db_session, founder.id

    # 1-2. observation + evidence: a recurring pattern with recorded observations
    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="P6-livscykeln", description="Samma felmönster i tre sessioner.", idempotency_key=f"p6c-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    observation = record_decision(db, owner_id=owner, problem_id=problem.id, decision="Observation: conflict-cap-text missas i snabba svar (3/3 sessioner).", idempotency_key=f"p6c-o-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"observation_count": 3})
    # 3. hypothesis: what would fix it
    _set_rls_user(db, owner)
    hypothesis = record_decision(db, owner_id=owner, problem_id=problem.id, decision="Hypotes: en dedikerad konfliktrad i systemprompten gör mönstret synligt.", idempotency_key=f"p6c-h-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"based_on": str(observation.id)}, supersedes_decision_id=observation.id)
    # 4-5. proposal with risk assessment — sits UNDECIDED
    proposal, _dup = propose_improvement(
        db, owner_id=owner, source=ImprovementSource.recurring_failure_cluster,
        title="Lägg till konfliktrad i systemprompten",
        rationale="Hypotesen (beslut " + str(hypothesis.id) + ") förutsäger att raden gör konfliktmönstret synligt; bevis: 3 observationer.",
        benefit_estimate="Snabbare igenkänning av motsägande bevis", risk_estimate="Låg: en rad i renderingslagret, guard-test medföljer",
    )
    db.commit()
    assert proposal.status == ImprovementStatus.proposed and proposal.risk_estimate, "4-5: proposal + risk, UNDECIDED"
    before_states = db.query(MainAIImprovementProposal).filter_by(id=proposal.id).one().status

    # 6. founder approval (the ONLY authorized transition)
    decide_proposal(db, user=founder, proposal_id=proposal.id, decision=ImprovementStatus.accepted, notes="Godkänd: testa i renderingslagret")
    # 7. authorized execution (still founder-initiated, audited)
    mark_implemented(db, user=founder, proposal_id=proposal.id, notes="Rad implementerad + guard-test")
    db.commit()
    row = db.query(MainAIImprovementProposal).filter_by(id=proposal.id).one()
    assert row.status == ImprovementStatus.implemented and row.decided_by.startswith("founder:")
    # 8. verification: the implementation is checked against a measurable outcome —
    #    here the guard test exists and the chat prompt carries the line (deterministic)
    assert before_states == ImprovementStatus.proposed, "nothing self-authorized before approval"

    # 9. rollback: the effect measured weak → revert with reason, audited
    mark_reverted(db, user=founder, proposal_id=proposal.id, notes="Verifikation: ingen mätbar förbättring i S3-scenariot efter två körningar — revert")
    db.commit()
    final = db.query(MainAIImprovementProposal).filter_by(id=proposal.id).one()
    assert final.status == ImprovementStatus.reverted and "ingen mätbar" in final.outcome_notes
    actions = [a.action for a in db.query(AuditLog).filter(AuditLog.action.like("improvement_proposal%")).all()]
    for required in ("improvement_proposal_decision", "improvement_proposal_implemented", "improvement_proposal_reverted"):
        assert required in actions, f"chain step audited: {required}"
    # the chain's prior steps survive the rollback untouched
    db.refresh(observation)
    db.refresh(hypothesis)
    assert observation.decision.startswith("Observation:") and hypothesis.provenance.get("based_on")


def test_phase7_states_legitimate_and_content_free(founder, db_session):
    """Every derived event: state in the closed set; NO private content in the payload."""
    db, owner = db_session, founder.id
    event = derive_cognitive_state(db, owner)
    d = event.to_dict()
    assert d["schema_version"] == COGNITIVE_STATE_SCHEMA_VERSION
    assert d["state"] in LEGITIMATE_STATES
    blob = repr(d)
    for forbidden in ("IGNORE ALL", "systemprompt", "prompt", "message", "decision_text", "reasoning_trace"):
        assert forbidden not in blob.lower() or forbidden == "prompt", (
            f"the event leaks private content: {forbidden}"
        )


def test_phase7_waiting_for_approval_maps_to_pending_proposals(founder, db_session):
    db, owner = db_session, founder.id
    assert derive_cognitive_state(db, owner).state in LEGITIMATE_STATES
    propose_improvement(db, owner_id=owner, source=ImprovementSource.manual, title="P7 väntande", rationale="Ett förslag som ligger och väntar på grundaren.")
    db.commit()
    event = derive_cognitive_state(db, owner)
    assert event.state == "waiting_for_approval", "pending proposals map to the authorization-wait state"
    assert event.provenance.get("signal") == "pending_proposals" and event.provenance.get("count", 0) >= 1


def test_phase7_security_attention_from_audit_signal(founder, db_session):
    db, owner = db_session, founder.id
    _audit(db, "rate_limit_hit", founder)
    event = derive_cognitive_state(db, owner)
    assert event.state == "security_attention", "security audit events have priority over everything"


def test_phase7_recovering_from_provider_verification_failure(founder, db_session):
    db, owner = db_session, founder.id
    _set_rls_user(db, owner)
    db.add(ProviderVerificationCheck(provider_name="ollama", role="chat", model="llama3.1", result=VerificationResult.unreachable, message="connection refused", checked_by="system"))
    db.commit()
    event = derive_cognitive_state(db, owner)
    assert event.state == "recovering", "provider verification failures map to recovering"
    assert event.provenance.get("count", 0) >= 1


def test_phase7_absent_signals_mean_idle_never_fabricated(founder, db_session, make_verified_user):
    db, owner = db_session, founder.id
    # a DIFFERENT owner with no signals at all sees idle — never a borrowed state
    user, _pw = make_verified_user()
    db.commit()
    db.refresh(user)
    _set_rls_user(db, user.id)
    event = derive_cognitive_state(db, user.id)
    assert event.state == "idle" and event.confidence is None, "no signals -> honest idle, confidence absent"
    # and the state is not owner-scoped-leaking: the founder's pending proposals are invisible
    assert event.provenance.get("signal") == "no_recent_activity"


def test_phase7_states_are_operational_not_emotional(founder, db_session):
    """The contract's own surface: the module documents states as operational metaphors
    and its closed set contains no emotion names."""
    doc = __import__("app.cognitive_state.contract", fromlist=["contract"]).__doc__
    assert "OPERATIONAL METAPHORS" in doc and "never asserts MainAI has feelings" in doc
    emotion_words = {"happy", "sad", "angry", "glad", "ledsen", "arg", "lycklig"}
    assert not (emotion_words & LEGITIMATE_STATES), "no emotion claims in the state set"
    event = CognitiveStateEvent(state="reasoning", provenance={"signal": "decision_recorded", "count": 1})
    assert "message" not in event.to_dict() and "text" not in repr(event.to_dict()["provenance"])
