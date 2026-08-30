"""Stage C — Memory → Work linkage scenarios (no authority widening)."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select, text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note, founder_correct_memory_note
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.memory_threads.service import thread_members
from app.memory_work_linkage import (
    ImpactKind,
    LinkageAction,
    TimingClass,
    apply_memory_work_linkage,
    assert_no_forbidden_imports,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.project_entities import ProjectEntityRelationship
from app.models.work_candidate import WorkCandidate
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.work_candidates import list_work_candidates


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


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


def _goal_plan_task(db, owner_id, *, task_description: str, instruction: str = "Ship memory carefully."):
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="Memory linkage goal",
        original_instruction=instruction,
        created_by="test",
    )
    plan = planner.create_plan(
        db,
        goal=goal,
        rationale="base",
        tasks=[PlannedTaskSpec(description=task_description, task_type="read_only_audit")],
        created_by="test",
    )
    db.flush()
    task = db.execute(select(MainAITask).where(MainAITask.plan_id == plan.id)).scalar_one()
    return goal, plan, task


def _auth_hash(goal) -> str:
    return hashlib.sha256(goal.original_instruction.encode()).hexdigest()


def test_assert_no_forbidden_imports():
    assert_no_forbidden_imports()


def test_1_founder_requirement_affects_active_task(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    entity_id = _promote_entity(
        db_session, owner_id=user.id, title="Postgres durable memory storage", key="t1-ent"
    )
    goal, plan, task = _goal_plan_task(
        db_session, user.id, task_description="Audit Postgres durable memory storage path"
    )
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Requirement: Postgres durable memory storage must keep history inspectable",
        note_type="decision",
        idempotency_key="t1-note",
    )
    db_session.commit()

    result = apply_memory_work_linkage(
        db_session,
        owner_id=user.id,
        note_id=note.id,
        timing=TimingClass.NOW,
        park_candidate=True,
    )
    db_session.commit()

    assert ImpactKind.AFFECTS_ACTIVE_TASK in result.impacts
    assert LinkageAction.LINKED_ONLY in result.actions
    assert any(a.kind == "mainai_task" and a.id == task.id for a in result.affected)
    assert any(a.kind == "project_entity" and a.id == entity_id for a in result.affected)
    # Memory alone does not insert executable work.
    assert result.created_task_ids == []
    members = thread_members(db_session, owner_id=user.id, thread_id=result.thread_id)
    kinds = {m.member_kind for m in members}
    assert "founder_memory_note" in kinds
    assert "mainai_task" in kinds


def test_2_founder_corrects_prior_requirement_supersedes_candidate(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    entity_id = _promote_entity(db_session, owner_id=user.id, title="Use MongoDB for sessions", key="t2-ent")
    original, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Use MongoDB for sessions",
        note_type="decision",
        idempotency_key="t2-orig",
    )
    first = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=original.id)
    db_session.commit()
    assert first.created_candidate_ids, "expected parked candidate for original decision"
    old_wc = first.created_candidate_ids[0]

    corrected, _ = founder_correct_memory_note(
        db_session,
        owner_id=user.id,
        note_id=original.id,
        content="Use Postgres for sessions instead of MongoDB",
        idempotency_key="t2-fix",
    )
    second = apply_memory_work_linkage(
        db_session,
        owner_id=user.id,
        note_id=corrected.id,
        is_correction=True,
        supersede_candidate_ids=[old_wc],
    )
    db_session.commit()

    assert ImpactKind.CORRECTION in second.impacts
    assert LinkageAction.CANDIDATE_SUPERSEDED in second.actions
    row = db_session.get(WorkCandidate, old_wc)
    assert row is not None
    assert row.status == "superseded"
    assert row.dismissed_reason.startswith("superseded_by_memory_note:")
    # History preserved: old candidate still inspectable.
    assert db_session.get(WorkCandidate, old_wc) is not None
    assert entity_id  # entity still present for park path


def test_3_same_requirement_different_wording_collapses(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(db_session, owner_id=user.id, title="Short founder answers preference", key="t3-ent")
    note_a, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="I want short founder answers",
        note_type="preference",
        idempotency_key="t3-a",
    )
    note_b, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="I want short founder answers!",
        note_type="preference",
        idempotency_key="t3-b",
    )
    db_session.commit()

    r1 = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note_a.id)
    db_session.commit()
    r2 = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note_b.id)
    db_session.commit()

    assert len(r1.created_candidate_ids) == 1
    assert r2.created_candidate_ids == []
    assert LinkageAction.NOOP_SAME in r2.actions
    assert ImpactKind.SAME_COLLAPSE in r2.impacts
    unreviewed = list_work_candidates(db_session, owner_id=user.id, status="unreviewed")
    memory_parked = [c for c in unreviewed if (c.title or "").startswith("[memory]")]
    assert len(memory_parked) == 1


def test_4_requirement_affects_completed_implementation_parks_followup(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(db_session, owner_id=user.id, title="Rate limit founder API", key="t4-ent")
    goal, plan, task = _goal_plan_task(
        db_session, user.id, task_description="Implement rate limit founder API"
    )
    task.status = MainAITaskStatus.completed
    from datetime import datetime

    task.completed_at = datetime.utcnow()
    db_session.flush()
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Also rate limit founder API on burst traffic",
        note_type="decision",
        idempotency_key="t4-note",
    )
    db_session.commit()

    result = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id)
    db_session.commit()

    assert ImpactKind.COMPLETED_FOLLOWUP in result.impacts
    assert result.created_task_ids == []
    assert len(result.created_candidate_ids) == 1
    wc = db_session.get(WorkCandidate, result.created_candidate_ids[0])
    assert wc.status == "unreviewed"


def test_5_requirement_later_not_now(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(db_session, owner_id=user.id, title="Quarterly archive export", key="t5-ent")
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Quarterly archive export should exist someday",
        note_type="goal",
        idempotency_key="t5-note",
    )
    db_session.commit()

    result = apply_memory_work_linkage(
        db_session,
        owner_id=user.id,
        note_id=note.id,
        timing=TimingClass.LATER,
    )
    db_session.commit()

    assert ImpactKind.PARK_LATER in result.impacts
    assert result.created_task_ids == []
    assert len(result.created_candidate_ids) == 1
    wc = db_session.get(WorkCandidate, result.created_candidate_ids[0])
    assert wc.priority == "low"
    assert wc.provenance.get("timing") == "later"
    assert wc.provenance.get("not_now") is True


def test_6_requirement_contradicts_current_plan(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    plan_entity = _promote_entity(db_session, owner_id=user.id, title="Keep single-region deploy", key="t6-plan")
    new_entity = _promote_entity(db_session, owner_id=user.id, title="Multi-region active-active", key="t6-new")
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Multi-region active-active is required now",
        note_type="decision",
        idempotency_key="t6-note",
    )
    db_session.commit()

    result = apply_memory_work_linkage(
        db_session,
        owner_id=user.id,
        note_id=note.id,
        contradict_entity_id=plan_entity,
    )
    db_session.commit()

    assert ImpactKind.CONTRADICTS_PLAN in result.impacts
    assert LinkageAction.CONTRADICTION_FLAGGED in result.actions
    edges = list(
        db_session.execute(
            select(ProjectEntityRelationship).where(
                ProjectEntityRelationship.owner_id == user.id,
                ProjectEntityRelationship.relationship_type == "contradicts",
            )
        ).scalars().all()
    )
    assert len(edges) == 1
    assert {edges[0].from_entity_id, edges[0].to_entity_id} == {plan_entity, new_entity}


def test_subordinate_insert_requires_authority_and_does_not_run_without_it(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    goal, plan, task = _goal_plan_task(
        db_session, user.id, task_description="Harden memory work linkage path"
    )
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Harden memory work linkage path with audit",
        note_type="decision",
        idempotency_key="t-auth",
    )
    db_session.commit()

    # Without authority flags: link only, no task insert.
    linked = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id)
    db_session.commit()
    assert linked.created_task_ids == []

    with pytest.raises(Exception):
        apply_memory_work_linkage(
            db_session,
            owner_id=user.id,
            note_id=note.id,
            insert_subordinate=True,
            authority_kind="idea",  # non-authoritative
            authorized_instruction_sha256=_auth_hash(goal),
            goal=goal,
            plan=plan,
        )

    inserted = apply_memory_work_linkage(
        db_session,
        owner_id=user.id,
        note_id=note.id,
        insert_subordinate=True,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        goal=goal,
        plan=plan,
        park_candidate=False,
    )
    db_session.commit()
    assert len(inserted.created_task_ids) == 1
    assert LinkageAction.SUBORDINATE_TASK_INSERTED in inserted.actions
    new_task = db_session.get(MainAITask, inserted.created_task_ids[0])
    assert new_task is not None
    assert new_task.id != task.id


def test_replay_is_idempotent_on_thread_membership(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Unique orphan observation about widgets",
        note_type="observation",
        idempotency_key="t-replay",
    )
    db_session.commit()
    a = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=False)
    b = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=False)
    db_session.commit()
    assert a.thread_id == b.thread_id
    members = thread_members(db_session, owner_id=user.id, thread_id=a.thread_id)
    note_members = [m for m in members if m.member_kind == "founder_memory_note"]
    assert len(note_members) == 1
