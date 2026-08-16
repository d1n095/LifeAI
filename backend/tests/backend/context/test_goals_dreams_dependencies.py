import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.active_context import create_context_set, pin_object
from app.life_intents import (
    IntentError,
    add_blocker,
    add_dependency,
    create_intent,
    evaluate_feasibility,
    list_actionable,
    resolve_blocker,
    transition_intent,
)
from app.memory_threads import add_member, create_thread
from app.models.active_context import ActiveContextMember
from app.models.life_intent import LifeIntent, LifeIntentEvent
from app.models.mainai_execution import MainAIGoal
from app.models.user import User


def _owner(db):
    owner = User(
        email=f"intent-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    return owner


def _intent(db, owner, key, kind="goal", state="active", basis="manual", **kwargs):
    return create_intent(
        db,
        owner_id=owner.id,
        title=key,
        intent_kind=kind,
        state=state,
        classification_basis=basis,
        authority="founder" if basis == "manual" else "unknown",
        idempotency_key=key,
        **kwargs,
    )


def test_kinds_states_provenance_and_history_are_distinct(superuser_db):
    owner = _owner(superuser_db)
    kinds = [
        _intent(superuser_db, owner, f"k-{k}", kind=k, basis="unknown")
        for k in ("goal", "dream", "need", "unknown")
    ]
    states = [
        _intent(superuser_db, owner, f"s-{s}", state=s)
        for s in (
            "active",
            "blocked",
            "waiting",
            "future",
            "completed",
            "abandoned",
            "superseded",
        )
    ]
    assert {x.intent_kind for x in kinds} == {"goal", "dream", "need", "unknown"}
    assert {x.state for x in states} == {
        "active",
        "blocked",
        "waiting",
        "future",
        "completed",
        "abandoned",
        "superseded",
    }
    inferred = _intent(superuser_db, owner, "inferred", kind="dream", basis="inferred")
    assert (
        inferred.classification_basis == "inferred"
        and kinds[0].classification_basis == "unknown"
    )
    transition_intent(
        superuser_db,
        owner_id=owner.id,
        intent_id=states[0].id,
        state="completed",
        reason="done",
    )
    assert states[0].state == "completed"
    assert (
        superuser_db.execute(
            select(LifeIntentEvent).where(LifeIntentEvent.intent_id == states[0].id)
        )
        .scalars()
        .all()
    )


def test_blocked_goal_does_not_freeze_unrelated_and_resolution_restores_actionability(
    superuser_db,
):
    owner = _owner(superuser_db)
    blocked = _intent(superuser_db, owner, "hardware")
    free = _intent(superuser_db, owner, "tests")
    blocker = add_blocker(
        superuser_db,
        owner_id=owner.id,
        intent_id=blocked.id,
        category="money",
        description="hardware purchase",
        basis="manual",
        idempotency_key="money",
    )
    assert not evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=blocked.id
    ).actionable
    assert evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=free.id
    ).actionable
    assert [x.id for x in list_actionable(superuser_db, owner_id=owner.id)] == [free.id]
    resolve_blocker(
        superuser_db,
        owner_id=owner.id,
        blocker_id=blocker.id,
        reason="budget available",
    )
    assert evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=blocked.id
    ).actionable
    assert (
        blocker.status == "resolved" and blocker.resolution_reason == "budget available"
    )


def test_required_dependencies_partial_branches_and_cycle_explanation(superuser_db):
    owner = _owner(superuser_db)
    root = _intent(superuser_db, owner, "build")
    blocked = _intent(superuser_db, owner, "deploy")
    free = _intent(superuser_db, owner, "local")
    add_blocker(
        superuser_db,
        owner_id=owner.id,
        intent_id=blocked.id,
        category="external_service",
        description="DNS",
        idempotency_key="dns",
    )
    add_dependency(
        superuser_db,
        owner_id=owner.id,
        from_intent_id=root.id,
        to_intent_id=blocked.id,
        relationship_type="requires",
        idempotency_key="root-deploy",
    )
    result = evaluate_feasibility(superuser_db, owner_id=owner.id, intent_id=root.id)
    assert not result.actionable and result.reasons[0]["path"] == (
        str(root.id),
        str(blocked.id),
    )
    assert evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=free.id
    ).actionable
    add_dependency(
        superuser_db,
        owner_id=owner.id,
        from_intent_id=blocked.id,
        to_intent_id=root.id,
        relationship_type="requires",
        idempotency_key="cycle",
    )
    result = evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=root.id, max_depth=5, max_nodes=10
    )
    assert result.cycles and any(
        r["reason"] == "dependency_cycle" for r in result.reasons
    )


def test_mainai_memory_thread_and_active_context_are_references_not_copies(
    superuser_db,
):
    owner = _owner(superuser_db)
    goal = MainAIGoal(
        owner_id=owner.id,
        title="execute",
        original_instruction="canonical",
        created_by="test",
    )
    superuser_db.add(goal)
    superuser_db.flush()
    thread = create_thread(
        superuser_db,
        owner_id=owner.id,
        idempotency_key="thread",
        manual_label="hardware",
        classification_basis="manual",
    )
    intent = _intent(
        superuser_db, owner, "buy", mainai_goal_id=goal.id, memory_thread_id=thread.id
    )
    member = add_member(
        superuser_db,
        owner_id=owner.id,
        thread_id=thread.id,
        member_kind="life_intent",
        member_ref_id=intent.id,
        membership_basis="explicit_reference",
    )
    context = create_context_set(
        superuser_db,
        owner_id=owner.id,
        anchor_type="life_intent",
        anchor_ref=str(intent.id),
        idempotency_key="ctx",
    )
    pin_object(
        superuser_db,
        owner_id=owner.id,
        context_set_id=context.id,
        object_type="life_intent_blocker",
        object_ref=add_blocker(
            superuser_db,
            owner_id=owner.id,
            intent_id=intent.id,
            category="hardware",
            description="computer",
            idempotency_key="computer",
        ).id,
    )
    assert (
        member.member_ref_id == str(intent.id)
        and goal.original_instruction == "canonical"
    )
    assert (
        superuser_db.execute(
            select(ActiveContextMember).where(
                ActiveContextMember.context_set_id == context.id
            )
        )
        .scalar_one()
        .object_type
        == "life_intent_blocker"
    )


def test_owner_reference_and_idempotency_fail_closed(superuser_db, db_session):
    a, b = _owner(superuser_db), _owner(superuser_db)
    ia = _intent(superuser_db, a, "a")
    ib = _intent(superuser_db, b, "b")
    with pytest.raises(IntentError):
        add_dependency(
            superuser_db,
            owner_id=a.id,
            from_intent_id=ia.id,
            to_intent_id=ib.id,
            relationship_type="requires",
            idempotency_key="cross",
        )
    replay = _intent(superuser_db, a, "same")
    assert _intent(superuser_db, a, "same").id == replay.id
    with pytest.raises(IntentError, match="idempotency"):
        create_intent(
            superuser_db, owner_id=a.id, title="different", idempotency_key="same"
        )
    superuser_db.commit()
    db_session.execute(
        text("SELECT set_config('app.current_user_id',:id,false)"), {"id": str(b.id)}
    )
    assert (
        db_session.execute(
            select(LifeIntent).where(LifeIntent.id == ia.id)
        ).scalar_one_or_none()
        is None
    )


def test_provider_independence_append_only_evidence_and_waiting_distinction(
    superuser_db, monkeypatch
):
    import app.providers.registry as registry

    monkeypatch.setattr(
        registry, "resolve_active", lambda *a, **k: pytest.fail("provider invoked")
    )
    owner = _owner(superuser_db)
    waiting = _intent(superuser_db, owner, "review", state="waiting")
    active = _intent(superuser_db, owner, "work")
    assert not evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=waiting.id
    ).actionable
    assert evaluate_feasibility(
        superuser_db, owner_id=owner.id, intent_id=active.id
    ).actionable
    superuser_db.commit()
    event = (
        superuser_db.execute(
            select(LifeIntentEvent).where(LifeIntentEvent.intent_id == waiting.id)
        )
        .scalars()
        .first()
    )
    event.detail = {"rewrite": True}
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()


def test_concurrent_state_updates_serialize(superuser_db):
    from sqlalchemy.orm import sessionmaker
    from app.db import migration_engine

    owner = _owner(superuser_db)
    intent = _intent(superuser_db, owner, "race")
    owner_id, intent_id = owner.id, intent.id
    superuser_db.commit()

    def update(reason):
        db = sessionmaker(bind=migration_engine)()
        try:
            transition_intent(
                db,
                owner_id=owner_id,
                intent_id=intent_id,
                state="completed",
                reason=reason,
            )
            db.commit()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, ["one", "two"]))
    events = (
        superuser_db.execute(
            select(LifeIntentEvent).where(
                LifeIntentEvent.intent_id == intent_id,
                LifeIntentEvent.event_type == "state_changed",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
