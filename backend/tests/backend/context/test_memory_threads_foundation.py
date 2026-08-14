import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.active_context import create_context_set, current_members
from app.memory_threads import (
    InvalidThreadOperation,
    add_member,
    add_relationship,
    branch_thread,
    create_thread,
    deactivate_member,
    expand_thread,
    link_thread_to_context,
    merge_threads,
    thread_members,
    update_thread_label,
)
from app.models.conversation import Conversation, Message, MessageRole
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.memory_thread import (
    MemoryThread,
    MemoryThreadEvent,
    MemoryThreadMember,
    MemoryThreadRelationship,
)
from app.models.user import User


def _owner(db):
    owner = User(
        email=f"thread-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    return owner


def _conversation(db, owner, title):
    conversation = Conversation(user_id=owner.id, title=title)
    db.add(conversation)
    db.flush()
    message = Message(
        conversation_id=conversation.id, role=MessageRole.user, content=f"truth:{title}"
    )
    db.add(message)
    db.flush()
    return conversation, message


def _task(db, owner):
    goal = MainAIGoal(
        owner_id=owner.id, title="goal", original_instruction="work", created_by="test"
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        rationale="plan",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="task",
        task_type="repo_edit",
    )
    db.add(task)
    db.flush()
    return goal, plan, task


def test_cross_conversation_heterogeneous_membership_provenance_and_source_truth(
    superuser_db,
):
    owner = _owner(superuser_db)
    c1, m1 = _conversation(superuser_db, owner, "one")
    c2, _ = _conversation(superuser_db, owner, "two")
    _, _, task = _task(superuser_db, owner)
    thread = create_thread(
        superuser_db,
        owner_id=owner.id,
        idempotency_key="life-os",
        manual_label="Life OS",
        classification_basis="manual",
    )
    rows = [
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=thread.id,
            member_kind="conversation",
            member_ref_id=c1.id,
            membership_basis="founder_added",
            classification_basis="manual",
        ),
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=thread.id,
            member_kind="conversation",
            member_ref_id=c2.id,
            membership_basis="unknown",
            classification_basis="unknown",
        ),
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=thread.id,
            member_kind="message",
            member_ref_id=m1.id,
            membership_basis="inferred",
            classification_basis="inferred",
        ),
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=thread.id,
            member_kind="mainai_task",
            member_ref_id=task.id,
            membership_basis="same_goal",
            classification_basis="deterministic",
        ),
    ]
    assert (
        len(thread_members(superuser_db, owner_id=owner.id, thread_id=thread.id)) == 4
    )
    assert {r.member_kind for r in rows} == {"conversation", "message", "mainai_task"}
    assert {r.membership_basis for r in rows} >= {
        "founder_added",
        "inferred",
        "unknown",
    }
    assert m1.content == "truth:one" and c1.title == "one"


def test_membership_replay_multi_thread_deactivation_and_audit(superuser_db):
    owner = _owner(superuser_db)
    conversation, _ = _conversation(superuser_db, owner, "shared")
    first = create_thread(superuser_db, owner_id=owner.id, idempotency_key="first")
    second = create_thread(superuser_db, owner_id=owner.id, idempotency_key="second")
    a = add_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=first.id,
        member_kind="conversation",
        member_ref_id=conversation.id,
        membership_basis="founder_added",
        classification_basis="manual",
        idempotency_key="member",
    )
    assert (
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=first.id,
            member_kind="conversation",
            member_ref_id=conversation.id,
            membership_basis="founder_added",
            classification_basis="manual",
            idempotency_key="member",
        ).id
        == a.id
    )
    with pytest.raises(InvalidThreadOperation, match="idempotency"):
        add_member(
            superuser_db,
            owner_id=owner.id,
            thread_id=first.id,
            member_kind="conversation",
            member_ref_id=conversation.id,
            membership_basis="inferred",
            idempotency_key="member",
        )
    add_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=second.id,
        member_kind="conversation",
        member_ref_id=conversation.id,
    )
    deactivate_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=first.id,
        member_kind="conversation",
        member_ref_id=conversation.id,
        reason="founder correction",
    )
    assert a.state == "inactive"
    assert superuser_db.execute(
        select(MemoryThreadEvent).where(
            MemoryThreadEvent.thread_id == first.id,
            MemoryThreadEvent.event_type == "member_deactivated",
        )
    ).scalar_one()


def test_owner_boundaries_relations_self_link_and_rls(superuser_db, db_session):
    a, b = _owner(superuser_db), _owner(superuser_db)
    ca, _ = _conversation(superuser_db, a, "a")
    ta = create_thread(superuser_db, owner_id=a.id, idempotency_key="a")
    tb = create_thread(superuser_db, owner_id=b.id, idempotency_key="b")
    with pytest.raises(InvalidThreadOperation):
        add_member(
            superuser_db,
            owner_id=b.id,
            thread_id=tb.id,
            member_kind="conversation",
            member_ref_id=ca.id,
        )
    with pytest.raises(InvalidThreadOperation):
        add_relationship(
            superuser_db,
            owner_id=a.id,
            from_thread_id=ta.id,
            to_thread_id=tb.id,
            relationship_type="related",
        )
    with pytest.raises(InvalidThreadOperation):
        add_relationship(
            superuser_db,
            owner_id=a.id,
            from_thread_id=ta.id,
            to_thread_id=ta.id,
            relationship_type="related",
        )
    superuser_db.commit()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(b.id)},
    )
    assert (
        db_session.execute(
            select(MemoryThread).where(MemoryThread.id == ta.id)
        ).scalar_one_or_none()
        is None
    )


def test_merge_and_branch_preserve_original_histories(superuser_db):
    owner = _owner(superuser_db)
    c1, _ = _conversation(superuser_db, owner, "one")
    c2, _ = _conversation(superuser_db, owner, "two")
    source = create_thread(superuser_db, owner_id=owner.id, idempotency_key="source")
    target = create_thread(superuser_db, owner_id=owner.id, idempotency_key="target")
    sm = add_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=source.id,
        member_kind="conversation",
        member_ref_id=c1.id,
    )
    add_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=target.id,
        member_kind="conversation",
        member_ref_id=c2.id,
    )
    merge_threads(
        superuser_db,
        owner_id=owner.id,
        source_thread_id=source.id,
        target_thread_id=target.id,
        idempotency_key="merge",
    )
    assert source.state == "superseded"
    assert {
        m.member_ref_id
        for m in thread_members(superuser_db, owner_id=owner.id, thread_id=source.id)
    } == {str(c1.id)}
    assert {
        m.member_ref_id
        for m in thread_members(superuser_db, owner_id=owner.id, thread_id=target.id)
    } == {str(c1.id), str(c2.id)}
    child = branch_thread(
        superuser_db,
        owner_id=owner.id,
        parent_thread_id=source.id,
        idempotency_key="branch",
        member_ids=[sm.id],
    )
    assert child.id != source.id and thread_members(
        superuser_db, owner_id=owner.id, thread_id=child.id
    )[0].member_ref_id == str(c1.id)
    relations = superuser_db.execute(select(MemoryThreadRelationship)).scalars().all()
    assert {r.relationship_type for r in relations} == {"merged_into", "branch"}


def test_bounded_cycle_safe_expansion_and_active_context_bridge(superuser_db):
    owner = _owner(superuser_db)
    goal, plan, task = _task(superuser_db, owner)
    thread = create_thread(superuser_db, owner_id=owner.id, idempotency_key="expand")
    shallow = expand_thread(
        superuser_db,
        owner_id=owner.id,
        thread_id=thread.id,
        anchor_kind="mainai_task",
        anchor_ref_id=task.id,
        max_depth=0,
    )
    assert {(m.member_kind, m.member_ref_id) for m in shallow} == {
        ("mainai_task", str(task.id))
    }
    full = expand_thread(
        superuser_db,
        owner_id=owner.id,
        thread_id=thread.id,
        anchor_kind="mainai_task",
        anchor_ref_id=task.id,
        max_depth=10,
        max_members=3,
        max_per_type=1,
    )
    assert len(full) == 3
    assert {m.member_ref_id for m in full} == {str(task.id), str(plan.id), str(goal.id)}
    context = create_context_set(
        superuser_db,
        owner_id=owner.id,
        anchor_type="mainai_task",
        anchor_ref=str(task.id),
        idempotency_key="context",
    )
    link_thread_to_context(
        superuser_db, owner_id=owner.id, thread_id=thread.id, context_set_id=context.id
    )
    current = current_members(
        superuser_db, owner_id=owner.id, context_set_id=context.id
    )
    assert [(m.object_type, m.object_ref) for m in current] == [
        ("memory_thread", str(thread.id))
    ]


def test_founder_correction_preserves_inference_and_ai_independence(
    superuser_db, monkeypatch
):
    import app.providers.registry as registry

    monkeypatch.setattr(
        registry, "resolve_active", lambda *a, **k: pytest.fail("provider invoked")
    )
    owner = _owner(superuser_db)
    thread = create_thread(
        superuser_db,
        owner_id=owner.id,
        idempotency_key="labels",
        system_label="phone project",
        classification_basis="inferred",
    )
    update_thread_label(
        superuser_db,
        owner_id=owner.id,
        thread_id=thread.id,
        label="Life OS privacy",
        basis="manual",
    )
    assert (
        thread.system_label == "phone project"
        and thread.manual_label == "Life OS privacy"
    )
    event = superuser_db.execute(
        select(MemoryThreadEvent).where(
            MemoryThreadEvent.thread_id == thread.id,
            MemoryThreadEvent.event_type == "label_changed",
        )
    ).scalar_one()
    assert event.detail["old"]["system_label"] == "phone project"
    superuser_db.commit()
    event.detail = {"rewrite": True}
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()


def test_database_vocabulary_owner_fk_and_duplicate_constraints(superuser_db):
    owner = _owner(superuser_db)
    thread = create_thread(
        superuser_db, owner_id=owner.id, idempotency_key="constraints"
    )
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        superuser_db.execute(
            text(
                "INSERT INTO memory_thread_members(thread_id,owner_id,member_kind,member_ref_id) VALUES(:t,:o,'invented',:r)"
            ),
            {"t": thread.id, "o": owner.id, "r": str(uuid.uuid4())},
        )
        superuser_db.commit()
    superuser_db.rollback()
    other = _owner(superuser_db)
    superuser_db.commit()
    with pytest.raises(IntegrityError):
        superuser_db.add(
            MemoryThreadMember(
                thread_id=thread.id,
                owner_id=other.id,
                member_kind="conversation",
                member_ref_id=str(uuid.uuid4()),
            )
        )
        superuser_db.commit()


def test_concurrent_thread_creation_converges(superuser_db):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    owner = _owner(superuser_db)
    owner_id = owner.id
    superuser_db.commit()

    def create_once():
        session = sessionmaker(bind=migration_engine)()
        try:
            thread = create_thread(
                session, owner_id=owner_id, idempotency_key="concurrent"
            )
            result = thread.id
            session.commit()
            return result
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create_once(), range(2)))
    assert ids[0] == ids[1]
    rows = (
        superuser_db.execute(
            select(MemoryThread).where(
                MemoryThread.owner_id == owner_id,
                MemoryThread.idempotency_key == "concurrent",
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [ids[0]]
