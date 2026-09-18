"""`app.mainai_workforce.mastery_ledger` + `promotion_policy` + `demotion_policy` +
`teacher_value` + `external_dependence`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InternalError

from app.mainai_workforce.demotion_policy import assess_demotion_trigger
from app.mainai_workforce.external_dependence import recommend_dependence_reduction
from app.mainai_workforce.mastery_ledger import (
    demote,
    get_or_create_mastery_record,
    list_mastery_events,
    promote,
    record_observation,
)
from app.mainai_workforce.promotion_policy import assess_promotion_eligibility
from app.mainai_workforce.teacher_value import assess_teacher_value
from app.mainai_workforce.types import AutonomyStage, ProviderDependenceRecommendation, WorkforceError
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"wf-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def _mastery(db, owner_id, task_class="backend_migration_debugging", provider="codex"):
    return get_or_create_mastery_record(db, owner_id=owner_id, capability_key="postgres_migration_debugging", task_class=task_class, external_teacher=provider, idempotency_key=f"m-{uuid.uuid4()}")


def test_get_or_create_is_idempotent_on_identity(superuser_db, owner_id):
    a = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="k", task_class="t", external_teacher="codex", idempotency_key="fixed-key")
    b = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="k", task_class="t", external_teacher="codex", idempotency_key="fixed-key-2")
    superuser_db.commit()
    assert a["id"] == b["id"]


def test_promotion_requires_new_stage_strictly_greater(superuser_db, owner_id):
    mastery = _mastery(superuser_db, owner_id)
    superuser_db.commit()
    with pytest.raises(WorkforceError):
        promote(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], new_stage=AutonomyStage.EXTERNAL_DEPENDENCY, reason="x")


def test_promotion_requires_non_empty_reason(superuser_db, owner_id):
    mastery = _mastery(superuser_db, owner_id)
    superuser_db.commit()
    with pytest.raises(WorkforceError):
        promote(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], new_stage=AutonomyStage.OBSERVE_EXTERNAL_EXPERT, reason="   ")


def test_promote_then_demote_records_both_events(superuser_db, owner_id):
    mastery = _mastery(superuser_db, owner_id)
    superuser_db.commit()
    promoted = promote(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], new_stage=AutonomyStage.EXECUTE_LOCAL_EXAMINER_EXTERNAL_SPOT_CHECKS, reason="8 diverse verified successes, 90% examiner pass rate")
    assert promoted["stage"] == int(AutonomyStage.EXECUTE_LOCAL_EXAMINER_EXTERNAL_SPOT_CHECKS)
    demoted = demote(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], new_stage=AutonomyStage.OBSERVE_EXTERNAL_EXPERT, reason="new architecture invalidated the old skill")
    assert demoted["stage"] == int(AutonomyStage.OBSERVE_EXTERNAL_EXPERT)
    superuser_db.commit()

    events = list_mastery_events(superuser_db, owner_id=owner_id, mastery_id=mastery["id"])
    assert [e["event_type"] for e in events] == ["promotion", "demotion"]


def test_mastery_events_are_append_only(superuser_db, owner_id):
    mastery = _mastery(superuser_db, owner_id)
    superuser_db.commit()
    promote(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], new_stage=AutonomyStage.OBSERVE_EXTERNAL_EXPERT, reason="first observation batch")
    superuser_db.commit()
    events = list_mastery_events(superuser_db, owner_id=owner_id, mastery_id=mastery["id"])
    with pytest.raises(InternalError):
        superuser_db.execute(
            text("UPDATE mainai_workforce_mastery_events SET reason='hacked' WHERE id=:id"),
            {"id": events[0]["id"]},
        )
        superuser_db.commit()
    superuser_db.rollback()


def test_record_observation_accumulates_counts(superuser_db, owner_id):
    mastery = _mastery(superuser_db, owner_id)
    superuser_db.commit()
    updated = record_observation(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], task_diversity_delta=1, local_practice=True, local_success=True, examiner_pass=True)
    superuser_db.commit()
    assert updated["observation_count"] == 1
    assert updated["local_success_count"] == 1
    assert updated["examiner_pass_count"] == 1
    assert updated["distinct_task_diversity"] == 1


def test_one_local_success_does_not_promote_mastery():
    assessment = assess_promotion_eligibility(observation_count=1, distinct_task_diversity=1, examiner_pass_rate=1.0, recency_days=0)
    assert assessment.eligible is False


def test_diverse_verified_successes_promote_mastery():
    assessment = assess_promotion_eligibility(observation_count=10, distinct_task_diversity=4, examiner_pass_rate=0.9, recency_days=5)
    assert assessment.eligible is True


def test_demotion_after_meaningful_regression():
    assessment = assess_demotion_trigger(recent_failure_rate_delta=0.3)
    assert assessment.triggered is True


def test_no_demotion_on_noise():
    assessment = assess_demotion_trigger(recent_failure_rate_delta=0.02)
    assert assessment.triggered is False


def test_external_agent_remains_because_it_catches_unique_bugs():
    promotion = assess_promotion_eligibility(observation_count=20, distinct_task_diversity=6, examiner_pass_rate=0.95, recency_days=2)
    teacher_value = assess_teacher_value(unique_capability_taught=False, novel_failure_discovery_count=3)
    recommendation = recommend_dependence_reduction(current_stage=AutonomyStage.LOCAL_DEFAULT_EXTERNAL_FALLBACK, promotion=promotion, teacher_value=teacher_value, quality_parity_with_external=True)
    assert recommendation.recommendation == ProviderDependenceRecommendation.DO_NOT_REDUCE_YET


def test_external_dependency_reduced_after_sustained_local_equivalence():
    promotion = assess_promotion_eligibility(observation_count=20, distinct_task_diversity=6, examiner_pass_rate=0.95, recency_days=2)
    teacher_value = assess_teacher_value(usage_count=20)
    recommendation = recommend_dependence_reduction(current_stage=AutonomyStage.LOCAL_DEFAULT_EXTERNAL_FALLBACK, promotion=promotion, teacher_value=teacher_value, quality_parity_with_external=True)
    assert recommendation.recommendation == ProviderDependenceRecommendation.MOVE_TO_FALLBACK


def test_one_expensive_day_does_not_remove_provider():
    promotion = assess_promotion_eligibility(observation_count=1, distinct_task_diversity=1, examiner_pass_rate=0.0, recency_days=0)
    teacher_value = assess_teacher_value(usage_count=1)
    recommendation = recommend_dependence_reduction(current_stage=AutonomyStage.EXTERNAL_DEPENDENCY, promotion=promotion, teacher_value=teacher_value, quality_parity_with_external=False)
    assert recommendation.recommendation == ProviderDependenceRecommendation.DO_NOT_REDUCE_YET
