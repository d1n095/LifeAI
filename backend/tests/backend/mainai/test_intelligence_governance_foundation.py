import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.intelligence_governance.service import (
    IdempotencyConflict,
    record_evidence,
    record_execution,
    record_idea,
    record_idea_link,
    record_interpretation,
)
from app.models.intelligence_governance import IntelligenceExecution, IntelligenceIdea
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _task(db, owner_id, *, task_type="repo_edit"):
    goal = MainAIGoal(
        owner_id=owner_id, title="problem", original_instruction="solve", created_by="test",
        completed_at=None,
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id,
                      description="candidate problem", task_type=task_type)
    db.add(task)
    db.flush()
    return task


def _owner(db):
    user = User(email=f"gov-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_versions_task_context_roles_and_multiple_candidates(superuser_db):
    owner = _owner(superuser_db)
    code_task = _task(superuser_db, owner.id, task_type="repo_edit")
    audit_task = _task(superuser_db, owner.id, task_type="read_only_audit")
    v1 = record_execution(superuser_db, owner_id=owner.id, task_id=code_task.id,
                          idempotency_key="v1", provider="vendor", model="agent", model_version="1",
                          role="builder", participation_mode="primary", task_type="repo_edit")
    v2 = record_execution(superuser_db, owner_id=owner.id, task_id=code_task.id,
                          idempotency_key="v2", provider="vendor", model="agent", model_version="2",
                          role="reviewer", participation_mode="parallel", task_type="repo_edit")
    other = record_execution(superuser_db, owner_id=owner.id, task_id=audit_task.id,
                             idempotency_key="other", provider="vendor", model="agent", model_version="2",
                             role="verifier", task_type="read_only_audit")
    superuser_db.commit()

    assert v1.model_version != v2.model_version
    assert v2.task_id == v1.task_id  # multiple candidate solutions for one problem
    assert other.task_type != v2.task_type  # same model, independently observable contexts
    assert {v1.role, v2.role, other.role} == {"builder", "reviewer", "verifier"}


def test_failed_execution_can_contribute_accepted_idea_and_rejected_is_preserved(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                                 idempotency_key="failed", role="builder")
    failure = record_evidence(superuser_db, owner_id=owner.id, execution_id=execution.id,
                              evidence_kind="outcome", payload={"status": "failed"},
                              source_type="mainai_task", source_ref=str(task.id), idempotency_key="outcome")
    accepted = record_idea(superuser_db, owner_id=owner.id, execution_id=execution.id,
                           evidence_id=failure.id, idea_kind="test_strategy", content="replay stale lease",
                           disposition="accepted", disposition_reason="caught race", idempotency_key="idea-a")
    rejected = record_idea(superuser_db, owner_id=owner.id, execution_id=execution.id,
                           evidence_id=failure.id, idea_kind="approach", content="trust worker name",
                           disposition="rejected", disposition_reason="not a fencing token", idempotency_key="idea-r")
    record_idea_link(superuser_db, owner_id=owner.id, from_idea_id=rejected.id,
                     to_idea_id=accepted.id, relation="contradicts", evidence_id=failure.id)
    superuser_db.commit()

    assert accepted.disposition == "accepted"
    stored_rejected = superuser_db.get(IntelligenceIdea, rejected.id)
    assert stored_rejected.content == "trust worker name"
    assert stored_rejected.evidence_id == failure.id


def test_raw_evidence_and_interpretation_are_separate_and_append_only(superuser_db):
    owner = _owner(superuser_db)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=_task(superuser_db, owner.id).id,
                                 idempotency_key="execution")
    evidence = record_evidence(superuser_db, owner_id=owner.id, execution_id=execution.id,
                               evidence_kind="test_result", payload={"passed": False, "tests": 12},
                               source_type="deterministic_verifier", source_ref="run-1",
                               review_kind="deterministic_tool", deterministic=True, idempotency_key="raw")
    interpretation = record_interpretation(
        superuser_db, owner_id=owner.id, evidence_id=evidence.id, interpretation_kind="quality_assessment",
        payload={"assessment": "promising despite failure"}, method="manual-review-v1",
        classification_basis="manual", confidence=0.6, idempotency_key="interpretation-1",
    )
    superuser_db.commit()
    assert interpretation.evidence_id == evidence.id
    assert evidence.payload == {"passed": False, "tests": 12}

    evidence.payload = {"passed": True}
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()
    superuser_db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.execute(text("DELETE FROM intelligence_evidence WHERE id=:id"), {"id": evidence.id})
        superuser_db.commit()


def test_self_independent_and_deterministic_review_attribution(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    builder = record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                               idempotency_key="builder", role="builder")
    reviewer = record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                                idempotency_key="reviewer", role="reviewer", participation_mode="reviewer")
    self_review = record_evidence(superuser_db, owner_id=owner.id, execution_id=builder.id,
                                  observer_execution_id=builder.id, evidence_kind="review", payload={},
                                  source_type="manual", source_ref="self", review_kind="self_review",
                                  idempotency_key="self")
    independent = record_evidence(superuser_db, owner_id=owner.id, execution_id=builder.id,
                                  observer_execution_id=reviewer.id, evidence_kind="review", payload={},
                                  source_type="model_review", source_ref="review-1",
                                  review_kind="independent_model", idempotency_key="independent")
    deterministic = record_evidence(superuser_db, owner_id=owner.id, execution_id=builder.id,
                                    evidence_kind="verification", payload={"passed": True},
                                    source_type="pytest", source_ref="run-2",
                                    review_kind="deterministic_tool", deterministic=True,
                                    idempotency_key="verify")
    superuser_db.commit()
    assert self_review.observer_execution_id == builder.id
    assert independent.observer_execution_id == reviewer.id
    assert deterministic.deterministic is True

    with pytest.raises(IntegrityError):
        record_evidence(superuser_db, owner_id=owner.id, execution_id=builder.id,
                        observer_execution_id=builder.id, evidence_kind="review", payload={},
                        source_type="model", source_ref="bad", review_kind="independent_model",
                        idempotency_key="bad-independent")


def test_unknown_ai_independence_and_idempotent_replay(superuser_db, monkeypatch):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                             idempotency_key="unknown", role="unknown",
                             classification_basis="unknown")
    replay = record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                              idempotency_key="unknown", role="unknown",
                              classification_basis="unknown")
    assert first.id == replay.id
    assert first.provider is None and first.model is None
    with pytest.raises(IdempotencyConflict):
        record_execution(superuser_db, owner_id=owner.id, task_id=task.id,
                         idempotency_key="unknown", model="changed")


def test_owner_foreign_keys_rls_and_no_winner_constraint(superuser_db, db_session):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    task_a, task_b = _task(superuser_db, owner_a.id), _task(superuser_db, owner_b.id)
    execution_a = record_execution(superuser_db, owner_id=owner_a.id, task_id=task_a.id,
                                   idempotency_key="a")
    superuser_db.commit()
    with pytest.raises(IntegrityError):
        record_execution(superuser_db, owner_id=owner_b.id, task_id=task_a.id,
                         idempotency_key="cross-owner")
    superuser_db.rollback()

    # RLS shows only the bound owner's rows through the same restricted role the app uses.
    db_session.execute(text("SELECT set_config('app.current_user_id', :owner, false)"), {"owner": str(owner_a.id)})
    assert db_session.execute(select(IntelligenceExecution)).scalars().all()[0].id == execution_a.id
    db_session.rollback()
    db_session.execute(text("SELECT set_config('app.current_user_id', :owner, false)"), {"owner": str(owner_b.id)})
    assert db_session.execute(select(IntelligenceExecution)).scalars().all() == []

    constraints = superuser_db.execute(text("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'intelligence_executions'::regclass
    """)).scalars().all()
    assert not any("winner" in value.lower() for value in constraints)
    assert task_b.owner_id == owner_b.id
