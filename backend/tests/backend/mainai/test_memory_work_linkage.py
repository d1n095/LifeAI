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
        link_to_work=False,
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
        link_to_work=False,
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
        link_to_work=False,
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
        link_to_work=False,
    )
    note_b, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="I want short founder answers!",
        note_type="preference",
        idempotency_key="t3-b",
        link_to_work=False,
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
        link_to_work=False,
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
        link_to_work=False,
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
        link_to_work=False,
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
        link_to_work=False,
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


def test_replay_with_park_candidate_is_idempotent_on_created_candidate_ids(db_session, make_verified_user):
    """P1 bug (memory_work_linkage, PR #211, live on integration tip):
    apply_memory_work_linkage(note_id=X) with park_candidate=True (the default) is NOT
    idempotent. First call: no similar unreviewed candidate exists, so one is created via
    record_work_candidate(); result includes CANDIDATE_RECORDED and
    created_candidate_ids=[wc_id]. Second identical call (a retry): _find_similar_unreviewed_
    candidate() now finds the candidate the FIRST call just created (same title/text,
    classifier_strategy="memory_work_linkage_v1") and takes the NOOP_SAME branch instead,
    returning created_candidate_ids=[] -- a caller keying off that field for
    notification/tracking silently loses it on retry."""
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(
        db_session, owner_id=user.id, title="Weekly cadence for the newsletter digest", key="t-retry-park-ent"
    )
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Founder wants weekly cadence for the newsletter digest",
        note_type="preference",
        idempotency_key="t-retry-park",
        link_to_work=False,
    )
    db_session.commit()

    first = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=True)
    db_session.commit()
    assert first.created_candidate_ids, "expected a parked candidate on the first call"
    first_wc_id = first.created_candidate_ids[0]
    assert LinkageAction.CANDIDATE_RECORDED in first.actions

    # Retry: identical call, same note_id -- must be idempotent, not a SAME-collapse NOOP.
    second = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=True)
    db_session.commit()

    assert second.created_candidate_ids == [first_wc_id], (
        f"retry lost the candidate id: expected [{first_wc_id}], got {second.created_candidate_ids} "
        f"(actions={second.actions})"
    )
    assert LinkageAction.NOOP_SAME not in second.actions
    # No duplicate candidate must have been created for this note.
    unreviewed = list_work_candidates(db_session, owner_id=user.id, status="unreviewed")
    memory_parked = [c for c in unreviewed if (c.title or "").startswith("[memory]")]
    assert len(memory_parked) == 1


def test_concurrent_similar_notes_do_not_double_park_same_collapse_candidate(db_session, make_verified_user, monkeypatch):
    """P1 race (memory_work_linkage, PR #211, live on integration tip):
    _find_similar_unreviewed_candidate() reads work_candidates with no locking before
    deciding whether to create a new one. Two genuinely concurrent calls for
    differently-worded-but-similar notes can both read before either commits and both create
    parked candidates, defeating the SAME-collapse guarantee
    docs/MAINAI_MEMORY_WORK_LINKAGE.md promises. Real two-thread/two-session test, not a
    simulated one: a threading.Barrier forces both sessions past the read into the
    create-or-collapse decision concurrently.

    IMPORTANT, found empirically while writing this test: apply_memory_work_linkage() calls
    ensure_linkage_thread() -> memory_threads.service.create_thread() FIRST, and that
    function takes `SELECT ... FOR UPDATE` on the OWNER's own users row ("Serialize first
    creation by owner so concurrent replay converges before the unique constraint is
    reached" -- its own comment) and holds it until the caller's own commit. That
    incidentally serializes the ENTIRE apply_memory_work_linkage() call per owner, masking
    this specific race end-to-end for any two calls sharing an owner_id -- confirmed via a
    debug trace showing thread 2 never even entering _find_similar_unreviewed_candidate
    until thread 1 had already left it. So this test bypasses ONLY that incidental lock
    (monkeypatching ensure_linkage_thread to do the same work without the owner-row lock),
    to isolate whether _find_similar_unreviewed_candidate's own missing locking is itself
    race-safe, independent of that unrelated mechanism -- exactly the gap
    docs/MAINAI_MEMORY_WORK_LINKAGE.md's SAME-collapse guarantee must not silently depend on
    a lock in a different module for its own correctness. A short sleep is injected between
    the read and its return to widen the window deterministically."""
    import threading
    import time

    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import sessionmaker

    import app.memory_work_linkage.service as mwl_service
    from app.memory_threads.service import add_member as _add_member
    from app.models.memory_thread import MemoryThread

    def _lockless_ensure_linkage_thread(db, *, owner_id, note_id):
        idem = f"memory-work-link:{note_id}"
        existing = db.execute(
            sa_select(MemoryThread).where(
                MemoryThread.owner_id == owner_id, MemoryThread.idempotency_key == idem
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = MemoryThread(
                owner_id=owner_id,
                idempotency_key=idem,
                system_label=f"memory_work_linkage:{note_id}",
                classification_basis="deterministic",
            )
            db.add(existing)
            db.flush()
        _add_member(
            db,
            owner_id=owner_id,
            thread_id=existing.id,
            member_kind="founder_memory_note",
            member_ref_id=note_id,
            membership_basis="founder_added",
            classification_basis="deterministic",
            provenance={"stage": "C", "role": "source_note"},
            idempotency_key=f"mem-link-note:{note_id}",
            actor_type="system",
        )
        return existing.id

    monkeypatch.setattr(mwl_service, "ensure_linkage_thread", _lockless_ensure_linkage_thread)

    _orig_find_similar = mwl_service._find_similar_unreviewed_candidate

    def _slow_find_similar(db, *, owner_id, text):
        result = _orig_find_similar(db, owner_id=owner_id, text=text)
        time.sleep(0.2)
        return result

    monkeypatch.setattr(mwl_service, "_find_similar_unreviewed_candidate", _slow_find_similar)

    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(db_session, owner_id=user.id, title="Weekly digest ships on Mondays", key="t-race-ent")
    note_a, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Founder wants the weekly digest to ship on Mondays",
        note_type="preference",
        idempotency_key="t-race-a",
        link_to_work=False,
    )
    note_b, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Founder wants the weekly digest to ship on Mondays!",
        note_type="preference",
        idempotency_key="t-race-b",
        link_to_work=False,
    )
    db_session.commit()
    owner_id = user.id
    note_a_id = note_a.id
    note_b_id = note_b.id

    bind = db_session.get_bind()
    Session = sessionmaker(bind=bind)
    barrier = threading.Barrier(2)
    results: list[tuple] = []

    def _attempt(label: str, note_id):
        session = Session()
        try:
            _set_rls_user(session, owner_id)
            barrier.wait(timeout=5)
            result = apply_memory_work_linkage(session, owner_id=owner_id, note_id=note_id, park_candidate=True)
            session.commit()
            results.append(("ok", label, result))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(("error", label, repr(exc)))
        finally:
            session.close()

    t1 = threading.Thread(target=_attempt, args=("a", note_a_id))
    t2 = threading.Thread(target=_attempt, args=("b", note_b_id))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    errors = [r for r in results if r[0] == "error"]
    assert errors == [], f"expected zero unhandled errors, got: {errors}"
    assert len(results) == 2

    db_session.expire_all()
    unreviewed = list_work_candidates(db_session, owner_id=owner_id, status="unreviewed")
    memory_parked = [c for c in unreviewed if (c.title or "").startswith("[memory]")]
    assert len(memory_parked) == 1, (
        f"SAME-collapse must serialize to exactly one parked candidate for two concurrent, "
        f"similar notes -- got {len(memory_parked)}: {[c.title for c in memory_parked]}"
    )


def test_replay_is_idempotent_on_thread_membership(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Unique orphan observation about widgets",
        note_type="observation",
        idempotency_key="t-replay",
        link_to_work=False,
    )
    db_session.commit()
    a = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=False)
    b = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id, park_candidate=False)
    db_session.commit()
    assert a.thread_id == b.thread_id
    members = thread_members(db_session, owner_id=user.id, thread_id=a.thread_id)
    note_members = [m for m in members if m.member_kind == "founder_memory_note"]
    assert len(note_members) == 1
