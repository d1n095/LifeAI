"""Real runtime scenarios A–F for composed MainAI executive behavior (safe internal)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note, founder_correct_memory_note
from app.mainai_executive import resume_executive_cycle, run_executive_cycle
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.workforce import record_verified_outcome, register_workforce_agent, score_candidates
from app.workforce.performance import get_or_create_rollup


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db):
    u = User(email=f"scen-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _entity(db, owner_id, title, key):
    document = Document(
        title="src",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text=title, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=owner_id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"prop-{key}",
    )
    db.flush()
    result = reconcile_and_promote_idea(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        title=title,
        entity_idempotency_key=f"entity-{key}",
    )
    db.flush()
    return result.canonical_entity_id


def test_scenario_a_shorthand_plan_workforce_restart(superuser_db):
    """A: ambiguous shorthand → plan → internal workforce → verify → memory → resume."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    entity = _entity(superuser_db, owner.id, "memory frontier glue", "a1")
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="gör samma grej med memory wiring",
        note_type="goal",
        idempotency_key=f"a-note-{uuid.uuid4()}",
    )
    sid = f"scen-a-{uuid.uuid4()}"
    first = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="gör samma grej där",
        source_entity_id=entity,
        note_id=note.id,
        session_id=sid,
    )
    assert first.workforce_dry_run["provider_invoked"] is False
    assert first.continuity_note_id is not None

    # Simulate process kill + resume from durable state only.
    resumed = resume_executive_cycle(superuser_db, owner_id=owner.id, session_id=sid)
    assert resumed["resumed"] is True
    assert resumed["authority_still_valid"] is False


def test_scenario_b_founder_correction_mid_plan(superuser_db):
    """B: correction mid-plan → superseded interpretation → no unauthorized execution."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    entity = _entity(superuser_db, owner.id, "auth path", "b1")
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="bygg provider activation nu",
        note_type="decision",
        idempotency_key=f"b-note-{uuid.uuid4()}",
    )
    sid = f"scen-b-{uuid.uuid4()}"
    run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="aktivera provider",
        source_entity_id=entity,
        note_id=note.id,
        session_id=sid,
        interruption_point="mid_plan",
    )
    corr, _ = founder_correct_memory_note(
        superuser_db,
        owner_id=owner.id,
        note_id=note.id,
        content="NEJ — endast safe internal, ingen provider",
        idempotency_key=f"b-corr-{uuid.uuid4()}",
    )
    after = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="endast safe internal",
        source_entity_id=entity,
        note_id=corr.id,
        session_id=sid,
    )
    assert after.workforce_dry_run is None or after.workforce_dry_run["provider_invoked"] is False
    assert "MEMORY_IS_NOT_AUTHORITY" in after.authority_denials


def test_scenario_c_overlap_reuses_existing(superuser_db):
    """C: related ideas days apart → reuse existing → missing piece not duplicate build."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    entity = _entity(superuser_db, owner.id, "workforce foundation", "c1")
    r1 = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="build workforce intelligence routing",
        source_entity_id=entity,
        session_id=f"scen-c1-{uuid.uuid4()}",
        run_workforce_dry=False,
    )
    r2 = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="add another workforce routing subsystem",
        source_entity_id=entity,
        session_id=f"scen-c2-{uuid.uuid4()}",
        run_workforce_dry=False,
    )
    assert r1.missing_pieces[0]["propose_new_subsystem"] is False
    assert r2.missing_pieces[0]["propose_new_subsystem"] is False
    assert "app.workforce" in r2.missing_pieces[0]["existing_modules"]


def test_scenario_d_interrupt_exact_continuation(superuser_db):
    """D: long task interrupted → restart → durable continuation, no invented authority."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    entity = _entity(superuser_db, owner.id, "long task", "d1")
    sid = f"scen-d-{uuid.uuid4()}"
    run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="lång körning",
        source_entity_id=entity,
        session_id=sid,
        interruption_point="after_delegation_before_result",
    )
    resumed = resume_executive_cycle(superuser_db, owner_id=owner.id, session_id=sid)
    assert resumed["resumed"] is True
    assert resumed["needs_founder_confirmation"] is True
    assert resumed["recovery"]["hallucinated_continuation"] is False


def test_scenario_e_poor_worker_changes_selection(superuser_db):
    """E: poor worker → verifier reject → performance ledger → next selection changes."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    weak = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key=f"weak-{uuid.uuid4().hex[:8]}",
        name="Weak",
        role="specialist",
        agent_type="LOCAL_MODEL",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["scen_e_cap"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    strong = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key=f"strong-{uuid.uuid4().hex[:8]}",
        name="Strong",
        role="specialist",
        agent_type="LOCAL_MODEL",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["scen_e_cap"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    # Record verified failure for weak, success for strong.
    get_or_create_rollup(superuser_db, owner_id=owner.id, profile_id=weak.id, capability_tag="scen_e_cap")
    get_or_create_rollup(superuser_db, owner_id=owner.id, profile_id=strong.id, capability_tag="scen_e_cap")
    record_verified_outcome(
        superuser_db,
        owner_id=owner.id,
        profile_id=weak.id,
        capability_tag="scen_e_cap",
        success=False,
    )
    record_verified_outcome(
        superuser_db,
        owner_id=owner.id,
        profile_id=strong.id,
        capability_tag="scen_e_cap",
        success=True,
    )
    ranked = score_candidates(superuser_db, owner_id=owner.id, required_capability="scen_e_cap")
    assert ranked[0].profile_id == strong.id


def test_scenario_f_cost_budget_prefers_cheaper(superuser_db):
    """F: tight cost → cheaper suitable worker selected."""
    owner = _owner(superuser_db)
    _set_rls(superuser_db, owner.id)
    cheap = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key=f"cheap-{uuid.uuid4().hex[:8]}",
        name="Cheap",
        role="specialist",
        agent_type="LOCAL_MODEL",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["scen_f_cap"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key=f"pricey-{uuid.uuid4().hex[:8]}",
        name="Pricey",
        role="specialist",
        agent_type="EXTERNAL_PROVIDER",
        trust_zone="EXTERNAL_PROVIDER",
        capability_tags=["scen_f_cap"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="high",
        status="active",
    )
    ranked = score_candidates(
        superuser_db,
        owner_id=owner.id,
        required_capability="scen_f_cap",
        prefer_local_only=True,
        cost_ceiling_usd=0.0,
    )
    assert ranked
    assert ranked[0].profile_id == cheap.id
