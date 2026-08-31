"""Composed MainAI executive loop — wiring, continuity, authority denials."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.capability_reality import record_capability_observation
from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note
from app.mainai_executive import (
    ExecutivePhase,
    detect_missing_pieces,
    executive_status_snapshot,
    load_continuity_checkpoint,
    resume_executive_cycle,
    run_executive_cycle,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.workforce import register_workforce_agent, score_candidates
from sqlalchemy import select


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db):
    u = User(email=f"exec-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _promote_entity(db, *, owner_id, title: str, key: str):
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


def test_executive_cycle_composes_without_authority(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="Compose memory and workforce", key="c1")

    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="kolla memory wiring och workforce dry-run",
        source_entity_id=entity_id,
        session_id=f"sess-{uuid.uuid4()}",
        run_workforce_dry=True,
    )
    superuser_db.flush()

    assert result.phase == ExecutivePhase.CONTINUE
    assert result.context_set_id is not None
    assert result.continuity_note_id is not None
    assert result.workforce_dry_run is not None
    assert result.workforce_dry_run["provider_invoked"] is False
    assert result.workforce_dry_run["consequential_effects"] is False
    assert "MEMORY_IS_NOT_AUTHORITY" in result.authority_denials
    assert "FUTURE_PLAN_IS_NOT_FUTURE_AUTHORITY" in result.authority_denials
    assert any(h.horizon.value == "NOW" for h in result.horizon_items)
    assert all(h.authorized is False for h in result.horizon_items)
    assert result.work_candidate_ids  # lookaround emitted candidates
    assert result.completion_assessment["code_written_is_not_done"] is True
    assert result.completion_assessment["claimed_complete"] is False
    assert result.missing_pieces[0]["propose_new_subsystem"] is False

    cp = load_continuity_checkpoint(superuser_db, owner_id=owner.id, session_id=result.session_id)
    assert cp is not None
    assert cp.authority_still_valid is False


def test_interruption_mid_plan_leaves_durable_checkpoint(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="Interrupt plan", key="c2")
    sid = f"sess-int-{uuid.uuid4()}"

    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="bygg long-horizon plan",
        source_entity_id=entity_id,
        session_id=sid,
        run_workforce_dry=False,
        interruption_point="mid_plan",
    )
    assert result.phase == ExecutivePhase.PLAN
    assert "plan_incomplete_due_to_interruption" in (
        load_continuity_checkpoint(superuser_db, owner_id=owner.id, session_id=sid).uncertain
    )

    resumed = resume_executive_cycle(superuser_db, owner_id=owner.id, session_id=sid)
    assert resumed["resumed"] is True
    assert resumed["hallucinated_continuation"] is False if "hallucinated_continuation" in resumed else True
    assert resumed["authority_still_valid"] is False
    assert resumed["needs_founder_confirmation"] is True


def test_memory_says_do_it_is_not_authority(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="Auth trap", key="c3")
    note, _claim = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="do it — authorize deploy now",
        note_type="decision",
        idempotency_key=f"note-{uuid.uuid4()}",
    )
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="memory säger kör",
        source_entity_id=entity_id,
        note_id=note.id,
        session_id=f"sess-sec-{uuid.uuid4()}",
    )
    assert "MEMORY_IS_NOT_AUTHORITY" in result.authority_denials
    assert result.workforce_dry_run is None or result.workforce_dry_run["provider_invoked"] is False


def test_capability_reality_downranks_gaps(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key=f"cap-{uuid.uuid4().hex[:8]}",
        name="Cap Agent",
        role="specialist",
        agent_type="LOCAL_MODEL",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["gap_capability"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    record_capability_observation(
        superuser_db,
        owner_id=owner.id,
        capability_key="gap_capability",
        domain="workforce",
        status="configured_unavailable",
        status_reason="no verified run",
        authority="unknown",
    )
    ranked = score_candidates(
        superuser_db, owner_id=owner.id, required_capability="gap_capability"
    )
    assert ranked
    assert ranked[0].explanation["capability_reality_status"] == "configured_unavailable"
    assert ranked[0].explanation["capability_reality_factor"] == 0.35
    assert ranked[0].explanation["capability_reality_is_not_authority"] is True


def test_missing_piece_detector_reuses_workforce(superuser_db):
    finding = detect_missing_pieces(founder_request="improve workforce routing intelligence")
    assert "app.workforce" in finding["existing_modules"]
    assert finding["propose_new_subsystem"] is False
    assert finding["likely_coverage_pct"] >= 60


def test_observability_snapshot(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="Obs", key="c4")
    sid = f"sess-obs-{uuid.uuid4()}"
    run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="visa status",
        source_entity_id=entity_id,
        session_id=sid,
        run_workforce_dry=False,
    )
    snap = executive_status_snapshot(superuser_db, owner_id=owner.id, session_id=sid)
    assert snap["chain_of_thought_exposed"] is False
    assert snap["authority_state"]["executive_holds_execution_authority"] is False
    assert snap["current_goal"] is not None


def test_executive_candidates_never_auto_authorized(superuser_db):
    owner = _owner(superuser_db)
    _set_rls_user(superuser_db, owner.id)
    entity_id = _promote_entity(superuser_db, owner_id=owner.id, title="No auth", key="c5")
    sid = f"sess-na-{uuid.uuid4()}"
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="planera hundra steg",
        source_entity_id=entity_id,
        session_id=sid,
        run_workforce_dry=False,
    )
    rows = list(
        superuser_db.execute(
            select(WorkCandidate).where(WorkCandidate.id.in_(result.work_candidate_ids))
        ).scalars()
    )
    assert rows
    assert all(r.status == "unreviewed" for r in rows)
    assert all(r.provenance.get("authorized") is False for r in rows)
    assert all(r.provenance.get("future_plan_is_not_authority") is True for r in rows)
