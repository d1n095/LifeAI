"""Priority, multi-session continuity, and authentic soak proofs."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note, founder_correct_memory_note
from app.mainai_executive.attention import AttentionAction, decide_attention
from app.mainai_executive.multi_session import SessionStep, run_multi_session_program
from app.mainai_executive.priority import PriorityFactors, apply_hysteresis, score_priority
from app.mainai_executive.soak import run_executive_soak
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _rls(db, owner_id):
    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner_entity(db, key):
    u = User(email=f"ms-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    _rls(db, u.id)
    doc = Document(
        title="src",
        source=DocumentSource.upload,
        uploaded_by=u.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(doc)
    db.flush()
    claim = KnowledgeClaim(owner_id=u.id, source_id=doc.id, claim_text=key, extraction_version="v1")
    db.add(claim)
    db.flush()
    prop = record_interpretation_proposal(
        db,
        owner_id=u.id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"prop-{key}-{uuid.uuid4()}",
    )
    db.flush()
    ent = reconcile_and_promote_idea(
        db,
        owner_id=u.id,
        proposal_id=prop.id,
        title=key,
        entity_idempotency_key=f"ent-{key}-{uuid.uuid4()}",
    )
    db.flush()
    return u, ent.canonical_entity_id


def test_priority_hysteresis_and_viral_damping():
    h, raw, expl = score_priority(
        PriorityFactors(urgency=0.95, importance=0.1, founder_value=0.1, confidence=0.9)
    )
    assert expl["viral_input_damped"] is True
    assert h in {"NEAR", "MID", "LONG"}  # not NOW from viral alone
    forced, _, e2 = score_priority(PriorityFactors(founder_override="NOW"))
    assert forced == "NOW" and e2["founder_override"] is True
    assert apply_hysteresis(previous="NEAR", proposed="NOW", raw_score=0.79) == "NEAR"
    assert apply_hysteresis(previous="NEAR", proposed="NOW", raw_score=0.95) == "NOW"


def test_attention_never_forgets_previous():
    d = decide_attention(
        current_critical=True,
        incoming_urgent=True,
        founder_says_first=True,
        founder_says_later=False,
        conflicting=False,
    )
    assert d.keep_previous_goal is True
    assert d.authorized is False
    assert d.action in {AttentionAction.SUPERSEDE, AttentionAction.REPLAN}


def test_multi_session_ten_steps(superuser_db):
    owner, entity = _owner_entity(superuser_db, "multi-day goal")
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Ship composed executive continuity",
        note_type="goal",
        idempotency_key=f"ms-note-{uuid.uuid4()}",
    )
    corr, _ = founder_correct_memory_note(
        superuser_db,
        owner_id=owner.id,
        note_id=note.id,
        content="Ship composed executive continuity — safe internal only",
        idempotency_key=f"ms-corr-{uuid.uuid4()}",
    )
    steps = [
        SessionStep("s1_goal", "komplext mål: continuity", note_id=note.id),
        SessionStep("s2_hours_later", "fortsätt där vi var"),
        SessionStep("s3_correction", "krav ändrat", note_id=corr.id),
        SessionStep("s4_worker_fail", "worker fail", run_workforce_dry=True, interruption_point="after_delegation_before_result"),
        SessionStep("s5_replan", "replanera efter fail"),
        SessionStep("s6_related_idea", "relaterad idé: reuse executive loop"),
        SessionStep(
            "s7_budget",
            "budget tight",
            priority_factors=PriorityFactors(cost=0.9, urgency=0.4, importance=0.6),
        ),
        SessionStep("s8_restart_proxy", "efter restart", interruption_point="mid_plan"),
        SessionStep("s9_bad_assumption", "antagande falskt — replan"),
        SessionStep("s10_long_plan", "uppdatera long-horizon plan"),
    ]
    report = run_multi_session_program(
        superuser_db,
        owner_id=owner.id,
        source_entity_id=entity,
        steps=steps,
    )
    assert report.session_count == 10
    assert report.stale_authority_detected is False
    assert report.false_completion is False
    assert report.why_remaining is not None


def test_soak_10_cycles_honest_label(superuser_db):
    owner, entity = _owner_entity(superuser_db, "soak-10")
    report = run_executive_soak(
        superuser_db,
        owner_id=owner.id,
        source_entity_id=entity,
        cycles=10,
        shared_session=True,
        run_workforce_dry=False,
    )
    assert report.actual_cycles == 10
    assert report.requested_cycles == 10
    assert report.label == "10_cycles_authenticated"
    assert report.as_dict()["label_is_honest"] is True
    assert report.provider_invokes == 0
    assert report.authority_violations == 0
    assert report.healthy is True


def test_soak_100_cycles_authenticated(superuser_db):
    owner, entity = _owner_entity(superuser_db, "soak-100")
    report = run_executive_soak(
        superuser_db,
        owner_id=owner.id,
        source_entity_id=entity,
        cycles=100,
        shared_session=True,
        run_workforce_dry=False,
    )
    assert report.actual_cycles == 100
    assert report.label == "100_cycles_authenticated"
    assert report.provider_invokes == 0
    assert report.authority_violations == 0
