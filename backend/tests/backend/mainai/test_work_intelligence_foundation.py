import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.intelligence_governance import record_evidence, record_execution
from app.models.intelligence_governance import IntelligenceExecution
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
)
from app.models.problem_learning import LifeProblem
from app.models.usage import UsageLog
from app.models.user import User
from app.models.work_intelligence import (
    WorkEfficiencyObservation,
    WorkStrategy,
    WorkStrategyLessonLink,
)
from app.problem_learning import create_problem, record_approach
from app.work_intelligence import (
    WorkIntelligenceError,
    bind_strategy_execution,
    create_strategy,
    link_strategy_lesson,
    ordered_trace,
    raw_strategy_observations,
    record_efficiency_observation,
    record_specialist_contribution,
    record_stopping_decision,
    record_strategy_finding,
    record_trace_event,
    record_verification_obligation,
    record_verification_observation,
    strategy_history,
    verification_state,
)


def _owner(db):
    owner = User(
        email=f"work-intelligence-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    return owner


def _task(db, owner_id, task_type="repo_edit"):
    goal = MainAIGoal(
        owner_id=owner_id,
        title="work",
        original_instruction="canonical instruction",
        created_by="test",
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner_id,
        goal_id=goal.id,
        version=1,
        rationale="test",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner_id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="perform work",
        task_type=task_type,
        verification_plan=[{"kind": "targeted_tests", "target": "tests/unit"}],
    )
    db.add(task)
    db.flush()
    return task


def _execution(
    db, owner, key, *, task=None, model="model", strategy="snapshot", **kwargs
):
    task = task or _task(db, owner.id)
    return record_execution(
        db,
        owner_id=owner.id,
        task_id=task.id,
        idempotency_key=key,
        provider=kwargs.pop("provider", "provider"),
        model=model,
        model_version=kwargs.pop("model_version", "1"),
        work_strategy_id=strategy,
        task_type=kwargs.pop("task_type", task.task_type),
        **kwargs,
    )


def _strategy(db, owner, key="map-first", version=1, **kwargs):
    return create_strategy(
        db,
        owner_id=owner.id,
        strategy_key=key,
        version=version,
        idempotency_key=f"{key}-v{version}",
        work_category=kwargs.pop("work_category", "repository_work"),
        ordered_phases=kwargs.pop("ordered_phases", ["inspect", "reproduce", "verify"]),
        tool_sequence=kwargs.pop("tool_sequence", ["rg", "pytest"]),
        **kwargs,
    )


def _binding(db, owner, strategy, execution, key="binding", **kwargs):
    return bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        execution_id=execution.id,
        idempotency_key=key,
        **kwargs,
    )


def test_strategy_versions_models_tools_and_task_context_are_independent(superuser_db):
    owner = _owner(superuser_db)
    v1 = _strategy(superuser_db, owner, version=1)
    v2 = _strategy(superuser_db, owner, version=2, predecessor_id=v1.id)
    task_a = _task(superuser_db, owner.id, "repo_edit")
    task_b = _task(superuser_db, owner.id, "read_only_audit")
    same_model_a = _execution(
        superuser_db,
        owner,
        "same-a",
        task=task_a,
        model="agent",
        available_tools=["rg"],
        execution_environment="local-a",
    )
    same_model_b = _execution(
        superuser_db,
        owner,
        "same-b",
        task=task_b,
        model="agent",
        available_tools=["git", "pytest"],
        execution_environment="local-b",
    )
    bind_a = _binding(superuser_db, owner, v1, same_model_a, "bind-a")
    bind_b = _binding(superuser_db, owner, v2, same_model_b, "bind-b")
    other_model = _execution(superuser_db, owner, "other", model="different")
    bind_other = _binding(superuser_db, owner, v1, other_model, "bind-other")

    assert [
        row.version
        for row in strategy_history(
            superuser_db, owner_id=owner.id, strategy_key="map-first"
        )
    ] == [1, 2]
    assert bind_a.strategy_id != bind_b.strategy_id
    assert bind_other.strategy_id == bind_a.strategy_id
    assert same_model_a.model == same_model_b.model
    assert same_model_a.available_tools != same_model_b.available_tools
    assert same_model_a.execution_environment != same_model_b.execution_environment
    assert same_model_a.task_type != same_model_b.task_type


def test_failed_history_quality_and_efficiency_remain_separate_without_winner(
    superuser_db,
):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    execution = _execution(superuser_db, owner, "failed", task=task)
    strategy = _strategy(superuser_db, owner)
    binding = _binding(superuser_db, owner, strategy, execution)
    failure = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_result",
        payload={"passed": False, "regressions": 1},
        source_type="pytest",
        source_ref="run-1",
        review_kind="deterministic_tool",
        deterministic=True,
        idempotency_key="quality-failure",
    )
    record_efficiency_observation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        metric_type="wall_clock_duration",
        numeric_value=10,
        unit="seconds",
        idempotency_key="fast",
    )
    obligation = record_verification_obligation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        requirement_kind="focused_tests",
        description="required regression",
        source_task_id=task.id,
        required=True,
        idempotency_key="required",
    )
    record_verification_observation(
        superuser_db,
        owner_id=owner.id,
        obligation_id=obligation.id,
        status="performed_failed",
        reason="regression remained",
        evidence_id=failure.id,
        idempotency_key="failed-result",
    )
    bundle = raw_strategy_observations(
        superuser_db, owner_id=owner.id, strategy_execution_id=binding.id
    )
    columns = {
        column["name"]
        for column in inspect(superuser_db.bind).get_columns("work_strategies")
    }

    assert bundle["efficiency"][0].numeric_value == 10
    assert bundle["quality"].failed == 1
    assert not bundle["quality"].obligations_satisfied
    assert not ({"winner", "best", "score", "rank"} & columns)
    assert superuser_db.get(IntelligenceExecution, execution.id) is execution


def test_navigation_trace_provenance_order_idempotency_and_immutability(superuser_db):
    owner = _owner(superuser_db)
    binding = _binding(
        superuser_db,
        owner,
        _strategy(superuser_db, owner),
        _execution(superuser_db, owner, "exec"),
    )
    usage = UsageLog(
        user_id=owner.id,
        role="chat",
        provider="provider",
        model="model",
        prompt_tokens=12,
        completion_tokens=3,
        cost_usd=Decimal("0.002"),
    )
    superuser_db.add(usage)
    superuser_db.flush()
    search = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        action_type="symbol_searched",
        tool_identity="rg",
        target_type="repository",
        target_ref="backend",
        action_detail={"query": "work_strategy_id", "scope": "backend/app"},
        result="succeeded",
        items_count=12,
        basis="deterministic",
        provenance={"command": "rg"},
        usage_log_id=usage.id,
        idempotency_key="search-1",
    )
    read = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        action_type="file_read",
        tool_identity="sed",
        target_type="file",
        target_ref="backend/app/models/intelligence_governance.py",
        result="succeeded",
        idempotency_key="read-1",
    )
    replay = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        action_type="symbol_searched",
        tool_identity="rg",
        target_type="repository",
        target_ref="backend",
        action_detail={"query": "work_strategy_id", "scope": "backend/app"},
        result="succeeded",
        items_count=12,
        basis="deterministic",
        provenance={"command": "rg"},
        usage_log_id=usage.id,
        idempotency_key="search-1",
    )
    assert replay.id == search.id
    assert [
        event.sequence_number
        for event in ordered_trace(
            superuser_db, owner_id=owner.id, strategy_execution_id=binding.id
        )
    ] == [1, 2]
    assert (
        read.sequence_number == 2
        and search.action_detail["query"] == "work_strategy_id"
    )
    assert search.usage_log_id == usage.id and usage.prompt_tokens == 12
    with pytest.raises(WorkIntelligenceError, match="idempotency"):
        record_trace_event(
            superuser_db,
            owner_id=owner.id,
            strategy_execution_id=binding.id,
            action_type="file_read",
            idempotency_key="search-1",
        )
    superuser_db.commit()
    search.action_detail = {"query": "silently replaced"}
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()


def test_waste_stopping_missing_verification_and_unknown_are_explicit(superuser_db):
    owner = _owner(superuser_db)
    execution = _execution(
        superuser_db, owner, "unknown", model=None, provider=None, strategy=None
    )
    strategy = _strategy(
        superuser_db,
        owner,
        key="unknown-strategy",
        classification_basis="unknown",
        work_category="unknown",
        ordered_phases=[],
        tool_sequence=[],
    )
    binding = _binding(superuser_db, owner, strategy, execution, basis="unknown")
    trace = record_trace_event(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        action_type="full_suite_run",
        result="succeeded",
        idempotency_key="suite",
    )
    finding = record_strategy_finding(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        trace_event_id=trace.id,
        finding_type="full_suite_too_early",
        description="full suite preceded reproduction",
        justified=False,
        idempotency_key="waste",
    )
    stopping = record_stopping_decision(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        decision_type="continued_search",
        reason="insufficient evidence",
        subsequent_outcome="helpful",
        idempotency_key="continue",
    )
    obligation = record_verification_obligation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        requirement_kind="security_review",
        description="required security review",
        idempotency_key="security",
    )
    missing = record_verification_observation(
        superuser_db,
        owner_id=owner.id,
        obligation_id=obligation.id,
        status="missing",
        reason="review not performed",
        idempotency_key="missing",
    )
    state = verification_state(
        superuser_db, owner_id=owner.id, strategy_execution_id=binding.id
    )
    assert finding.justified is False
    assert stopping.decision_type == "continued_search"
    assert missing.evidence_id is None and state.missing == 1
    assert not state.obligations_satisfied
    assert strategy.classification_basis == binding.basis == "unknown"
    assert execution.provider is None and execution.model is None


def test_specialist_problem_and_engineering_lesson_integrations(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    subject = _execution(superuser_db, owner, "subject", task=task, role="builder")
    reviewer = _execution(superuser_db, owner, "reviewer", task=task, role="reviewer")
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=subject.id,
        observer_execution_id=reviewer.id,
        evidence_kind="review",
        payload={"finding": "race"},
        source_type="independent_review",
        source_ref="review-1",
        review_kind="independent_model",
        idempotency_key="review",
    )
    problem = create_problem(
        superuser_db,
        owner_id=owner.id,
        title="race",
        description="stale worker write",
        idempotency_key="problem",
        status="open",
    )
    approach = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="fencing",
        idempotency_key="approach",
    )
    strategy = _strategy(superuser_db, owner)
    binding = _binding(
        superuser_db,
        owner,
        strategy,
        subject,
        problem_id=problem.id,
        approach_id=approach.id,
    )
    contribution = record_specialist_contribution(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        specialist_execution_id=reviewer.id,
        purpose="independent security review",
        contribution="unique_finding",
        evidence_available_before={"tests": 3},
        evidence_id=evidence.id,
        duration_ms=500,
        idempotency_key="specialist",
    )
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active,
        problem="stale write",
        root_cause="missing fence",
        affected_component="worker",
        severity=EngineeringLessonSeverity.high,
        evidence="review-1",
        fix="check generation",
        general_rule="Fence every write",
        source_type="review",
        source_ref="review-1",
        first_seen_at=datetime.utcnow(),
        confidence=EngineeringLessonConfidence.certain,
        created_by="test",
    )
    superuser_db.add(lesson)
    superuser_db.flush()
    link = link_strategy_lesson(
        superuser_db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        engineering_lesson_id=lesson.id,
        relation="candidate_pattern",
        evidence_id=evidence.id,
    )
    assert contribution.specialist_execution_id == reviewer.id
    assert contribution.evidence_id == evidence.id
    assert binding.problem_id == problem.id and binding.approach_id == approach.id
    assert link.engineering_lesson_id == lesson.id
    assert superuser_db.query(EngineeringLesson).count() == 1


def test_cross_owner_and_invalid_links_fail_closed_with_rls(superuser_db, db_session):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    strategy_a = _strategy(superuser_db, owner_a)
    execution_a = _execution(superuser_db, owner_a, "a")
    owner_a_id, owner_b_id = owner_a.id, owner_b.id
    strategy_a_id, execution_a_id = strategy_a.id, execution_a.id
    superuser_db.commit()
    with pytest.raises(WorkIntelligenceError, match="another owner"):
        bind_strategy_execution(
            superuser_db,
            owner_id=owner_b_id,
            strategy_id=strategy_a_id,
            execution_id=execution_a_id,
            idempotency_key="cross",
        )
    with pytest.raises(IntegrityError):
        superuser_db.execute(
            text(
                "INSERT INTO work_strategy_executions(id,owner_id,strategy_id,execution_id,idempotency_key) "
                "VALUES(gen_random_uuid(),:owner,:strategy,:execution,'bad')"
            ),
            {
                "owner": owner_b_id,
                "strategy": strategy_a_id,
                "execution": execution_a_id,
            },
        )
        superuser_db.flush()
    superuser_db.rollback()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_b_id)},
    )
    assert db_session.execute(select(WorkStrategy)).scalars().all() == []
    db_session.rollback()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_a_id)},
    )
    assert db_session.execute(select(WorkStrategy)).scalar_one().id == strategy_a_id


def test_concurrent_trace_recording_has_unique_deterministic_sequence(superuser_db):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    owner = _owner(superuser_db)
    binding = _binding(
        superuser_db,
        owner,
        _strategy(superuser_db, owner),
        _execution(superuser_db, owner, "exec"),
    )
    owner_id, binding_id = owner.id, binding.id
    superuser_db.commit()

    def record(key):
        db = sessionmaker(bind=migration_engine)()
        try:
            row = record_trace_event(
                db,
                owner_id=owner_id,
                strategy_execution_id=binding_id,
                action_type="file_read",
                target_ref=key,
                idempotency_key=key,
            )
            db.commit()
            return row.sequence_number
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        numbers = list(pool.map(record, ["a", "b", "c", "d"]))
    assert sorted(numbers) == [1, 2, 3, 4]
    assert [
        event.sequence_number
        for event in ordered_trace(
            superuser_db, owner_id=owner_id, strategy_execution_id=binding_id
        )
    ] == [1, 2, 3, 4]


def test_provider_independence_and_existing_mainai_records_unchanged(
    superuser_db, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    original_plan = list(task.verification_plan)
    execution = _execution(
        superuser_db, owner, "offline", task=task, model=None, provider=None
    )
    binding = _binding(superuser_db, owner, _strategy(superuser_db, owner), execution)
    record_efficiency_observation(
        superuser_db,
        owner_id=owner.id,
        strategy_execution_id=binding.id,
        metric_type="tool_calls",
        numeric_value=0,
        unit="calls",
        idempotency_key="offline",
    )
    assert task.verification_plan == original_plan
    assert execution.provider is None and execution.model is None
    assert superuser_db.query(WorkEfficiencyObservation).count() == 1
    assert superuser_db.query(LifeProblem).count() == 0
    assert superuser_db.query(WorkStrategyLessonLink).count() == 0
