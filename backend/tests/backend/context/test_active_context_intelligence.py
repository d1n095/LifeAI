import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.active_context import (
    InvalidContextReference,
    create_context_set,
    current_members,
    mark_noncurrent,
    pin_object,
    refresh_context,
    suppress_object,
    unpin_object,
    unsuppress_object,
)
from app.intelligence_governance import record_evidence, record_execution, record_idea
from app.models.active_context import ActiveContextEvent, ActiveContextMember, ActiveContextSet
from app.models.conversation import Conversation, Message, MessageRole
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.mainai_job import MainAIJob
from app.models.user import User


def _owner(db):
    user = User(email=f"context-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id, *, with_job=False):
    goal = MainAIGoal(owner_id=owner_id, title="goal", original_instruction="work", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="plan", created_by="test")
    db.add(plan)
    db.flush()
    job = None
    if with_job:
        job = MainAIJob(owner_id=owner_id, job_type="corpus_review", created_by="test")
        db.add(job)
        db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id,
                      description="task", task_type="repo_edit", mainai_job_id=job.id if job else None)
    db.add(task)
    db.flush()
    return goal, plan, task, job


def _context(db, owner_id, typ, ref, key="ctx", basis="deterministic"):
    return create_context_set(db, owner_id=owner_id, anchor_type=typ, anchor_ref=str(ref),
                              idempotency_key=key, subject_basis=basis)


def _keys(members):
    return {(m.object_type, m.object_ref) for m in members}


def test_message_anchor_activates_conversation_without_mutating_sources(superuser_db):
    owner = _owner(superuser_db)
    conversation = Conversation(user_id=owner.id, title="Original title")
    superuser_db.add(conversation)
    superuser_db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="Original truth")
    superuser_db.add(message)
    superuser_db.flush()
    context = _context(superuser_db, owner.id, "message", message.id)

    first = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    second = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    superuser_db.commit()

    assert _keys(first) == _keys(second) == {
        ("message", str(message.id)), ("conversation", str(conversation.id))
    }
    assert len(first) == 2
    assert message.content == "Original truth" and conversation.title == "Original title"
    conversation_member = next(m for m in first if m.object_type == "conversation")
    assert conversation_member.inclusion_reason == "same_conversation"
    assert conversation_member.activation_path[-1]["relation"] == "same_conversation"


def test_task_anchor_plan_goal_limits_dedup_and_cycle_safety(superuser_db):
    owner = _owner(superuser_db)
    goal, plan, task, job = _task(superuser_db, owner.id, with_job=True)
    context = _context(superuser_db, owner.id, "mainai_task", task.id)

    depth_zero = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id, max_depth=0)
    assert _keys(depth_zero) == {("mainai_task", str(task.id))}
    bounded = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id,
                              max_depth=5, max_members=3, max_per_type=1)
    assert len(bounded) == 3
    full = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id,
                           max_depth=10, max_members=20)
    keys = _keys(full)
    assert {("mainai_task", str(task.id)), ("mainai_plan", str(plan.id)),
            ("mainai_goal", str(goal.id)), ("mainai_job", str(job.id))} <= keys
    assert len(keys) == len(full)  # task -> job -> task cycle did not duplicate or run away


def test_intelligence_idea_and_evidence_trace_to_execution_and_task(superuser_db):
    owner = _owner(superuser_db)
    _, _, task, _ = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="execution")
    evidence = record_evidence(superuser_db, owner_id=owner.id, execution_id=execution.id,
                               evidence_kind="finding", payload={"verified": True}, source_type="test",
                               source_ref="run-1", idempotency_key="evidence")
    idea = record_idea(superuser_db, owner_id=owner.id, execution_id=execution.id,
                       evidence_id=evidence.id, idea_kind="risk", content="bounded context",
                       idempotency_key="idea")
    context = _context(superuser_db, owner.id, "intelligence_idea", idea.id)
    members = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)

    keys = _keys(members)
    assert ("intelligence_evidence", str(evidence.id)) in keys
    assert ("intelligence_execution", str(execution.id)) in keys
    assert ("mainai_task", str(task.id)) in keys
    task_member = next(m for m in members if m.object_type == "mainai_task")
    assert [step["relation"] for step in task_member.activation_path] == ["anchor", "originated_in", "same_task"]


def test_manual_pin_and_suppression_survive_refresh(superuser_db):
    owner = _owner(superuser_db)
    conversation = Conversation(user_id=owner.id, title="thread")
    superuser_db.add(conversation)
    superuser_db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.user, content="source")
    superuser_db.add(message)
    superuser_db.flush()
    _, _, unrelated_task, _ = _task(superuser_db, owner.id)
    context = _context(superuser_db, owner.id, "message", message.id)

    pinned = pin_object(superuser_db, owner_id=owner.id, context_set_id=context.id,
                        object_type="mainai_task", object_ref=unrelated_task.id)
    suppress_object(superuser_db, owner_id=owner.id, context_set_id=context.id,
                    object_type="conversation", object_ref=conversation.id)
    members = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    assert pinned.state == "pinned"
    assert ("mainai_task", str(unrelated_task.id)) in _keys(members)
    assert ("conversation", str(conversation.id)) not in _keys(members)
    assert unpin_object(superuser_db, owner_id=owner.id, context_set_id=context.id,
                        object_type="mainai_task", object_ref=unrelated_task.id).state == "active"

    unsuppress_object(superuser_db, owner_id=owner.id, context_set_id=context.id,
                      object_type="conversation", object_ref=conversation.id)
    members = refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    assert ("conversation", str(conversation.id)) in _keys(members)
    actions = superuser_db.execute(select(ActiveContextEvent.action).where(
        ActiveContextEvent.context_set_id == context.id)).scalars().all()
    assert {"pinned", "suppressed", "unsuppressed", "refreshed"} <= set(actions)


def test_unknown_manual_inferred_deterministic_and_noncurrent_states(superuser_db):
    owner = _owner(superuser_db)
    _, _, task, _ = _task(superuser_db, owner.id)
    unknown = _context(superuser_db, owner.id, "mainai_task", task.id, key="unknown", basis="unknown")
    manual = create_context_set(superuser_db, owner_id=owner.id, anchor_type="explicit_topic",
                                anchor_ref="Founder-selected topic", idempotency_key="manual",
                                subject_basis="manual")
    inferred = _context(superuser_db, owner.id, "mainai_task", task.id, key="inferred", basis="inferred")
    deterministic = _context(superuser_db, owner.id, "mainai_task", task.id, key="det", basis="deterministic")
    assert {unknown.subject_basis, manual.subject_basis, inferred.subject_basis,
            deterministic.subject_basis} == {"unknown", "manual", "inferred", "deterministic"}

    refresh_context(superuser_db, owner_id=owner.id, context_set_id=unknown.id)
    mark_noncurrent(superuser_db, owner_id=owner.id, context_set_id=unknown.id,
                    object_type="mainai_task", object_ref=task.id, state="superseded",
                    reason="newer founder decision")
    assert ("mainai_task", str(task.id)) not in _keys(
        current_members(superuser_db, owner_id=owner.id, context_set_id=unknown.id)
    )
    history = current_members(superuser_db, owner_id=owner.id, context_set_id=unknown.id, include_noncurrent=True)
    assert next(m for m in history if m.object_type == "mainai_task").state == "superseded"


def test_invalid_cross_owner_refs_rls_and_fk_fail_closed(superuser_db, db_session):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    _, _, task_a, _ = _task(superuser_db, owner_a.id)
    with pytest.raises(InvalidContextReference):
        _context(superuser_db, owner_b.id, "mainai_task", task_a.id)
    with pytest.raises(InvalidContextReference):
        _context(superuser_db, owner_a.id, "made_up", task_a.id)
    with pytest.raises(InvalidContextReference):
        _context(superuser_db, owner_a.id, "mainai_task", "not-a-uuid")

    context = _context(superuser_db, owner_a.id, "mainai_task", task_a.id)
    refresh_context(superuser_db, owner_id=owner_a.id, context_set_id=context.id)
    superuser_db.commit()
    db_session.execute(text("SELECT set_config('app.current_user_id', :id, false)"), {"id": str(owner_b.id)})
    assert db_session.execute(select(ActiveContextSet)).scalars().all() == []
    db_session.rollback()

    bad = ActiveContextMember(context_set_id=context.id, owner_id=owner_b.id, object_type="mainai_task",
                              object_ref=str(task_a.id), inclusion_reason="bad", relevance_basis="manual",
                              activation_path=[], source_provenance={})
    superuser_db.add(bad)
    with pytest.raises(IntegrityError):
        superuser_db.commit()
    superuser_db.rollback()
    with pytest.raises(DBAPIError):
        superuser_db.execute(text("""
            INSERT INTO active_context_members
                (context_set_id, owner_id, object_type, object_ref, inclusion_reason,
                 relevance_basis, activation_path, source_provenance)
            VALUES (:context, :owner, 'made_up', :ref, 'bad', 'manual', '[]', '{}')
        """), {"context": context.id, "owner": owner_a.id, "ref": str(task_a.id)})
        superuser_db.commit()


def test_provider_independence_and_event_immutability(superuser_db, monkeypatch):
    owner = _owner(superuser_db)
    _, _, task, _ = _task(superuser_db, owner.id)
    import app.providers.registry as registry

    monkeypatch.setattr(registry, "resolve_active", lambda *a, **k: pytest.fail("provider invoked"))
    context = _context(superuser_db, owner.id, "mainai_task", task.id)
    refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    pin_object(superuser_db, owner_id=owner.id, context_set_id=context.id,
               object_type="mainai_task", object_ref=task.id)
    superuser_db.commit()
    event = superuser_db.execute(select(ActiveContextEvent).where(
        ActiveContextEvent.context_set_id == context.id)).scalars().first()
    event.detail = {"rewritten": True}
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()


def test_concurrent_refreshes_serialize_without_duplicate_members(superuser_db):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    owner = _owner(superuser_db)
    _, _, task, _ = _task(superuser_db, owner.id)
    context = _context(superuser_db, owner.id, "mainai_task", task.id)
    owner_id, context_id = owner.id, context.id
    superuser_db.commit()

    def refresh_once():
        session = sessionmaker(bind=migration_engine)()
        try:
            result = refresh_context(session, owner_id=owner_id, context_set_id=context_id)
            session.commit()
            return len(result)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: refresh_once(), range(2)))
    assert counts == [3, 3]
    rows = superuser_db.execute(select(ActiveContextMember).where(
        ActiveContextMember.context_set_id == context_id)).scalars().all()
    assert len(rows) == len(_keys(rows)) == 3


def test_owner_erasure_cascades_context_index_and_audit(superuser_db):
    owner = _owner(superuser_db)
    _, _, task, _ = _task(superuser_db, owner.id)
    context = _context(superuser_db, owner.id, "mainai_task", task.id)
    refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    context_id = context.id
    superuser_db.commit()

    # Simulate the final users-row removal after domain-specific erasure children are gone.
    superuser_db.execute(text("DELETE FROM active_context_sets WHERE owner_id=:owner"), {"owner": owner.id})
    superuser_db.commit()
    assert superuser_db.get(ActiveContextSet, context_id) is None
    assert superuser_db.execute(select(ActiveContextEvent).where(
        ActiveContextEvent.context_set_id == context_id)).scalars().all() == []
