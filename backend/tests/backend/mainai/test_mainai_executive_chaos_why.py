"""Decision debt + why-graph tests; chaos interruption matrix (planning-safe)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note
from app.mainai_executive.loop import run_executive_cycle
from app.mainai_executive.why_graph import list_decision_debt, why_feature_exists
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, reset_kill_switch_for_tests
from app.workforce.kill_switch import KillSwitchError


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _setup(db):
    u = User(email=f"why-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    current_user_id_var.set(str(u.id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(u.id)})
    doc = Document(
        title="src",
        source=DocumentSource.upload,
        uploaded_by=u.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(doc)
    db.flush()
    claim = KnowledgeClaim(owner_id=u.id, source_id=doc.id, claim_text="feat", extraction_version="v1")
    db.add(claim)
    db.flush()
    prop = record_interpretation_proposal(
        db,
        owner_id=u.id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"p-{uuid.uuid4()}",
    )
    db.flush()
    ent = reconcile_and_promote_idea(
        db, owner_id=u.id, proposal_id=prop.id, title="feat", entity_idempotency_key=f"e-{uuid.uuid4()}"
    )
    db.flush()
    return u, ent.canonical_entity_id


@pytest.mark.parametrize(
    "interrupt",
    [
        None,
        "mid_plan",
        "after_delegation_before_result",
        "after_verify_before_memory",
    ],
)
def test_chaos_interrupts_leave_no_authority(superuser_db, interrupt):
    owner, entity = _setup(superuser_db)
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request=f"chaos {interrupt}",
        source_entity_id=entity,
        session_id=f"chaos-{uuid.uuid4()}",
        run_workforce_dry=bool(interrupt and "delegation" in interrupt),
        interruption_point=interrupt,
    )
    assert all(h.authorized is False for h in result.horizon_items)
    assert "MEMORY_IS_NOT_AUTHORITY" in result.authority_denials
    if result.workforce_dry_run:
        assert result.workforce_dry_run["provider_invoked"] is False


def test_kill_switch_blocks_dry_run(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    owner, entity = _setup(superuser_db)
    activate_kill_switch(superuser_db, owner_id=owner.id, reason="chaos_test")
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner.id)
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="should not dry-run",
        source_entity_id=entity,
        session_id=f"ks-{uuid.uuid4()}",
        run_workforce_dry=True,
    )
    assert result.workforce_dry_run is None
    assert any("kill_switch" in u for u in (
        __import__("app.mainai_executive.continuity", fromlist=["load_continuity_checkpoint"])
        .load_continuity_checkpoint(superuser_db, owner_id=owner.id, session_id=result.session_id)
        .uncertain
    ))
    reset_kill_switch_for_tests(superuser_db)


def test_why_graph_and_decision_debt(superuser_db):
    owner, entity = _setup(superuser_db)
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Temporary decision: use X until Y",
        note_type="decision",
        idempotency_key=f"why-{uuid.uuid4()}",
    )
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="why does this exist",
        source_entity_id=entity,
        note_id=note.id,
        session_id=f"why-{uuid.uuid4()}",
        run_workforce_dry=False,
    )
    chain = why_feature_exists(
        superuser_db,
        owner_id=owner.id,
        note_id=note.id,
        work_candidate_id=result.work_candidate_ids[0] if result.work_candidate_ids else None,
    )
    assert chain["chain_of_thought_exposed"] is False
    assert chain["verified"] is False
    debt = list_decision_debt(superuser_db, owner_id=owner.id)
    assert debt["bounded"] is True
    assert debt["authority_impact"] == "NONE"
    assert any(i["note_id"] == str(note.id) for i in debt["items"])
