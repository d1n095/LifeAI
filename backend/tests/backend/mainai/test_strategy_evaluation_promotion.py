import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.intelligence_governance import record_evidence, record_execution, record_idea
from app.models.intelligence_governance import IntelligenceIdea
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.strategy_evaluation import (
    StrategyComparison,
    StrategyEvaluationEvent,
    StrategyPromotionCandidate,
)
from app.models.user import User
from app.models.work_intelligence import WorkEfficiencyObservation, WorkStrategy
from app.strategy_evaluation import (
    StrategyEvaluationError,
    assess_comparability,
    assess_quality,
    create_comparison,
    create_experiment,
    create_promotion_candidate,
    link_comparison,
    link_learning_subject,
    record_efficiency_delta,
    record_learning_observation,
    transition_candidate,
    transition_experiment,
)
from app.work_intelligence import (
    bind_strategy_execution,
    create_strategy,
    record_efficiency_observation,
    record_stopping_decision,
    record_trace_event,
    record_verification_obligation,
    record_verification_observation,
)


def _owner(db):
    row = User(
        email=f"strategy-eval-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(row)
    db.flush()
    return row


def _task(db, owner, task_type="repo_edit"):
    goal = MainAIGoal(
        owner_id=owner.id,
        title="evaluate",
        original_instruction="truth",
        created_by="test",
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        rationale="test",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="work",
        task_type=task_type,
        verification_plan=[],
    )
    db.add(task)
    db.flush()
    return task


def _pair(
    db,
    owner,
    *,
    model_a="same",
    model_b="same",
    env_a="local",
    env_b="local",
    task_type="repo_edit",
):
    task = _task(db, owner, task_type)
    strategy_key = f"workflow-{uuid.uuid4()}"
    baseline = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key=strategy_key,
        version=1,
        idempotency_key=f"base-{uuid.uuid4()}",
    )
    challenger = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key=strategy_key,
        version=2,
        predecessor_id=baseline.id,
        idempotency_key=f"challenger-{uuid.uuid4()}",
    )
    execution_a = record_execution(
        db,
        owner_id=owner.id,
        task_id=task.id,
        provider="provider-a",
        model=model_a,
        model_version="1",
        execution_environment=env_a,
        task_type=task_type,
        idempotency_key=f"exec-a-{uuid.uuid4()}",
    )
    execution_b = record_execution(
        db,
        owner_id=owner.id,
        task_id=task.id,
        provider="provider-b",
        model=model_b,
        model_version="2",
        execution_environment=env_b,
        task_type=task_type,
        idempotency_key=f"exec-b-{uuid.uuid4()}",
    )
    binding_a = bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=baseline.id,
        execution_id=execution_a.id,
        idempotency_key=f"bind-a-{uuid.uuid4()}",
    )
    binding_b = bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=challenger.id,
        execution_id=execution_b.id,
        idempotency_key=f"bind-b-{uuid.uuid4()}",
    )
    comparison = create_comparison(
        db,
        owner_id=owner.id,
        baseline_binding_id=binding_a.id,
        challenger_binding_id=binding_b.id,
        task_id=task.id,
        task_type=task_type,
        domain="engineering",
        comparison_basis="deterministic",
        idempotency_key=f"compare-{uuid.uuid4()}",
    )
    return (
        task,
        baseline,
        challenger,
        execution_a,
        execution_b,
        binding_a,
        binding_b,
        comparison,
    )


def _verify(db, owner, binding, key, *, passed=True):
    evidence = record_evidence(
        db,
        owner_id=owner.id,
        execution_id=binding.execution_id,
        evidence_kind="test_result",
        review_kind="deterministic_tool",
        deterministic=True,
        payload={"passed": passed},
        source_type="pytest",
        source_ref=key,
        idempotency_key=f"evidence-{key}",
    )
    obligation = record_verification_obligation(
        db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        requirement_kind="focused_tests",
        description="required tests",
        idempotency_key=f"obligation-{key}",
    )
    record_verification_observation(
        db,
        owner_id=owner.id,
        obligation_id=obligation.id,
        status="performed_passed" if passed else "performed_failed",
        reason="observed",
        evidence_id=evidence.id,
        idempotency_key=f"verification-{key}",
    )
    return evidence


def test_versions_models_strategy_and_context_are_independent(superuser_db):
    owner = _owner(superuser_db)
    (
        _,
        baseline,
        challenger,
        execution_a,
        execution_b,
        binding_a,
        binding_b,
        comparison,
    ) = _pair(
        superuser_db, owner, model_a="same", model_b="same", env_a="old", env_b="new"
    )
    assert baseline.version == 1 and challenger.version == 2
    assert execution_a.model == execution_b.model
    assert execution_a.execution_environment != execution_b.execution_environment
    assert binding_a.strategy_id != binding_b.strategy_id
    assert comparison.task_type == "repo_edit"
    other_owner = _owner(superuser_db)
    other_task, same_strategy, _, other_execution, _, _, _, _ = _pair(
        superuser_db, other_owner, model_a="different"
    )
    assert other_execution.model != execution_a.model
    assert same_strategy.version == baseline.version
    assert other_task.task_type == comparison.task_type


def test_quality_gate_fails_closed_and_efficiency_is_raw(superuser_db):
    owner = _owner(superuser_db)
    _, _, _, _, _, baseline_binding, challenger_binding, comparison = _pair(
        superuser_db, owner
    )
    _verify(superuser_db, owner, baseline_binding, "base", passed=True)
    incomplete = assess_quality(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        subject="challenger",
        reason="missing",
        idempotency_key="q-incomplete",
    )
    assert incomplete.state == "verification_incomplete"
    failed_evidence = _verify(
        superuser_db, owner, challenger_binding, "challenge", passed=False
    )
    failed = assess_quality(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        subject="challenger",
        reason="failed",
        evidence_id=failed_evidence.id,
        idempotency_key="q-failed",
    )
    assert failed.state == "quality_fail"
    base_eff = record_efficiency_observation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=baseline_binding.id,
        metric_type="wall_clock_duration",
        numeric_value=40,
        unit="minutes",
        idempotency_key="base-time",
    )
    challenge_eff = record_efficiency_observation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=challenger_binding.id,
        metric_type="wall_clock_duration",
        numeric_value=20,
        unit="minutes",
        idempotency_key="challenge-time",
    )
    delta = record_efficiency_delta(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        baseline_observation_id=base_eff.id,
        challenger_observation_id=challenge_eff.id,
        idempotency_key="delta",
    )
    assert delta.delta_value == -20
    assert superuser_db.get(WorkEfficiencyObservation, base_eff.id).numeric_value == 40
    candidate = create_promotion_candidate(
        superuser_db,
        owner_id=owner.id,
        strategy_id=challenger_binding.strategy_id,
        baseline_strategy_id=baseline_binding.strategy_id,
        minimum_valid_comparisons=1,
        idempotency_key="candidate",
    )
    assess_comparability(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        status="comparable",
        dimensions={},
        reasons=[],
        idempotency_key="comparable",
    )
    link_comparison(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        comparison_id=comparison.id,
        idempotency_key="candidate-link",
    )
    _, _ = transition_candidate(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        to_state="under_review",
        idempotency_key="review",
    )
    with pytest.raises(StrategyEvaluationError, match="quality-safe"):
        transition_candidate(
            superuser_db,
            owner_id=owner.id,
            candidate_id=candidate.id,
            to_state="approved",
            idempotency_key="approve",
        )


def test_comparability_statuses_and_invalid_evidence_counts(superuser_db):
    owner = _owner(superuser_db)
    rows = []
    for status in ("comparable", "partially_comparable", "not_comparable", "unknown"):
        _, _, _, _, _, _, _, comparison = _pair(superuser_db, owner)
        rows.append(
            assess_comparability(
                superuser_db,
                owner_id=owner.id,
                comparison_id=comparison.id,
                status=status,
                dimensions={"task_type": "same"},
                reasons=[status],
                basis="manual",
                idempotency_key=f"status-{status}",
            )
        )
    assert {row.status for row in rows} == {
        "comparable",
        "partially_comparable",
        "not_comparable",
        "unknown",
    }


def test_failed_challenger_preserves_useful_and_rejected_ideas(superuser_db):
    owner = _owner(superuser_db)
    _, _, _, _, challenger_execution, _, _, comparison = _pair(superuser_db, owner)
    useful = record_idea(
        superuser_db,
        owner_id=owner.id,
        execution_id=challenger_execution.id,
        idea_kind="test_strategy",
        content="narrow first",
        disposition="accepted",
        disposition_reason="verified useful sub-idea",
        idempotency_key="useful",
    )
    rejected = record_idea(
        superuser_db,
        owner_id=owner.id,
        execution_id=challenger_execution.id,
        idea_kind="approach",
        content="skip tests",
        disposition="rejected",
        disposition_reason="unsafe",
        idempotency_key="rejected",
    )
    useful_link = link_learning_subject(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        idea_id=useful.id,
        disposition="useful",
        relation="accepted_component",
        reason="reduced waste",
        idempotency_key="useful-link",
    )
    rejected_link = link_learning_subject(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        idea_id=rejected.id,
        disposition="rejected",
        relation="rejected_component",
        reason="quality loss",
        idempotency_key="rejected-link",
    )
    assert useful_link.disposition == "useful"
    assert rejected_link.disposition == "rejected"
    assert superuser_db.get(IntelligenceIdea, rejected.id).disposition == "rejected"


def test_experiment_and_promotion_are_auditable_without_activation(superuser_db):
    owner = _owner(superuser_db)
    _, baseline, challenger, _, _, base_binding, challenge_binding, comparison = _pair(
        superuser_db, owner
    )
    experiment = create_experiment(
        superuser_db,
        owner_id=owner.id,
        baseline_strategy_id=baseline.id,
        challenger_strategy_id=challenger.id,
        hypothesis="less search, same proof",
        intended_change="narrow first",
        expected_benefit="fewer reads",
        quality_invariants=["tests pass"],
        required_sample_count=2,
        idempotency_key="experiment",
    )
    link_comparison(
        superuser_db,
        owner_id=owner.id,
        experiment_id=experiment.id,
        comparison_id=comparison.id,
        idempotency_key="experiment-link",
    )
    for state in ("ready", "running", "completed"):
        experiment, _ = transition_experiment(
            superuser_db,
            owner_id=owner.id,
            experiment_id=experiment.id,
            to_state=state,
            idempotency_key=f"experiment-{state}",
        )
    assert experiment.state == "completed"
    assert (
        superuser_db.execute(
            select(StrategyEvaluationEvent).where(
                StrategyEvaluationEvent.experiment_id == experiment.id
            )
        )
        .scalars()
        .all()
    )
    candidate = create_promotion_candidate(
        superuser_db,
        owner_id=owner.id,
        strategy_id=challenger.id,
        baseline_strategy_id=baseline.id,
        minimum_valid_comparisons=1,
        idempotency_key="promotion",
    )
    link_comparison(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        comparison_id=comparison.id,
        idempotency_key="promotion-link",
    )
    _verify(superuser_db, owner, base_binding, "audit-base")
    _verify(superuser_db, owner, challenge_binding, "audit-challenge")
    assess_quality(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        subject="challenger",
        reason="all passed",
        idempotency_key="quality-pass",
    )
    assess_comparability(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        status="comparable",
        dimensions={},
        reasons=[],
        idempotency_key="fair",
    )
    transition_candidate(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        to_state="under_review",
        idempotency_key="under-review",
    )
    approved, _ = transition_candidate(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        to_state="approved",
        idempotency_key="approved",
    )
    assert approved.state == "approved"
    assert superuser_db.get(WorkStrategy, challenger.id).id == challenger.id
    assert not hasattr(challenger, "active")


def test_search_and_stopping_learning_references_canonical_trace(superuser_db):
    owner = _owner(superuser_db)
    _, _, _, _, _, _, challenge_binding, comparison = _pair(superuser_db, owner)
    first = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=challenge_binding.id,
        action_type="symbol_searched",
        target_ref="needle",
        result="no_result",
        idempotency_key="search-1",
    )
    second = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=challenge_binding.id,
        action_type="symbol_searched",
        target_ref="needle",
        result="no_result",
        idempotency_key="search-2",
    )
    stop = record_stopping_decision(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=challenge_binding.id,
        decision_type="stopped_search",
        reason="bounded attempts",
        subsequent_outcome="helpful",
        idempotency_key="stop",
    )
    observation = record_learning_observation(
        superuser_db,
        owner_id=owner.id,
        comparison_id=comparison.id,
        observation_type="duplicate_search",
        trace_event_id=first.id,
        related_trace_event_id=second.id,
        stopping_decision_id=stop.id,
        numeric_value=2,
        unit="queries",
        reason="same target repeated",
        basis="deterministic",
        idempotency_key="navigation",
    )
    assert observation.trace_event_id == first.id
    assert observation.stopping_decision_id == stop.id


def test_idempotency_cross_owner_constraints_rls_and_provider_independence(
    superuser_db, db_session
):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    _, _, _, _, _, baseline, challenger, comparison = _pair(
        superuser_db, owner_a, model_a=None, model_b=None
    )
    replay = create_comparison(
        superuser_db,
        owner_id=owner_a.id,
        baseline_binding_id=baseline.id,
        challenger_binding_id=challenger.id,
        task_id=comparison.task_id,
        task_type=comparison.task_type,
        domain=comparison.domain,
        comparison_basis="deterministic",
        idempotency_key=comparison.idempotency_key,
    )
    assert replay.id == comparison.id
    with pytest.raises(StrategyEvaluationError, match="different fields"):
        create_comparison(
            superuser_db,
            owner_id=owner_a.id,
            baseline_binding_id=challenger.id,
            challenger_binding_id=baseline.id,
            idempotency_key=comparison.idempotency_key,
        )
    with pytest.raises(StrategyEvaluationError, match="another owner"):
        assess_comparability(
            superuser_db,
            owner_id=owner_b.id,
            comparison_id=comparison.id,
            status="unknown",
            dimensions={},
            reasons=[],
            idempotency_key="cross",
        )
    owner_a_id, owner_b_id, comparison_id = owner_a.id, owner_b.id, comparison.id
    superuser_db.commit()
    with pytest.raises(IntegrityError):
        superuser_db.execute(
            text(
                "INSERT INTO strategy_quality_assessments(id,owner_id,comparison_id,subject,state,required_count,passed_count,failed_count,missing_count,reason,idempotency_key) VALUES(gen_random_uuid(),:owner,:comparison,'challenger','unknown',0,0,0,0,'bad','cross')"
            ),
            {"owner": owner_b_id, "comparison": comparison_id},
        )
        superuser_db.flush()
    superuser_db.rollback()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_b_id)},
    )
    assert db_session.execute(select(StrategyComparison)).scalars().all() == []
    db_session.rollback()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_a_id)},
    )
    assert (
        db_session.execute(select(StrategyComparison)).scalar_one().id == comparison_id
    )


def test_no_schema_level_winner_and_historical_candidate_remains(superuser_db):
    owner = _owner(superuser_db)
    _, baseline, challenger, _, _, _, _, _ = _pair(superuser_db, owner)
    candidate = create_promotion_candidate(
        superuser_db,
        owner_id=owner.id,
        strategy_id=challenger.id,
        baseline_strategy_id=baseline.id,
        idempotency_key="history",
    )
    rejected, _ = transition_candidate(
        superuser_db,
        owner_id=owner.id,
        candidate_id=candidate.id,
        to_state="rejected",
        idempotency_key="reject",
    )
    assert rejected.state == "rejected"
    assert (
        superuser_db.get(StrategyPromotionCandidate, candidate.id).state == "rejected"
    )
    assert "winner" not in StrategyPromotionCandidate.__table__.columns


def test_concurrent_observation_replay_does_not_duplicate(superuser_db):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    owner = _owner(superuser_db)
    _, _, _, _, _, _, _, comparison = _pair(superuser_db, owner)
    owner_id, comparison_id = owner.id, comparison.id
    superuser_db.commit()

    def record():
        db = sessionmaker(bind=migration_engine)()
        try:
            row = record_learning_observation(
                db,
                owner_id=owner_id,
                comparison_id=comparison_id,
                observation_type="successful_narrowing",
                reason="bounded search sequence",
                basis="deterministic",
                idempotency_key="concurrent-replay",
            )
            row_id = row.id
            db.commit()
            return row_id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: record(), range(2)))
    assert len(set(ids)) == 1
