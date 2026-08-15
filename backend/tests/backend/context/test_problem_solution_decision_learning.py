import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.active_context import create_context_set, pin_object
from app.intelligence_governance import record_evidence, record_execution, record_idea
from app.memory_threads import add_member, create_thread
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
)
from app.models.problem_learning import (
    LifeComponentEvaluation,
    LifeProblem,
    LifeProblemDecision,
    LifeProblemEvent,
    LifeProblemLessonLink,
    LifeSolutionSelection,
)
from app.models.user import User
from app.problem_learning import (
    ProblemLearningError,
    active_decision,
    approaches_for_problem,
    create_problem,
    evaluate_component,
    link_components,
    link_engineering_lesson,
    open_problems,
    record_approach,
    record_assumption,
    record_component,
    record_decision,
    record_outcome,
    select_component,
    transition_approach,
    transition_assumption,
    transition_problem,
    unverified_assumptions,
    useful_components_from_failed_approaches,
)


def _owner(db):
    owner = User(
        email=f"problem-learning-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    return owner


def _task(db, owner_id):
    goal = MainAIGoal(
        owner_id=owner_id,
        title="execution goal",
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
        description="implement",
        task_type="repo_edit",
    )
    db.add(task)
    db.flush()
    return task


def _problem(db, owner, key="problem", **values):
    return create_problem(
        db,
        owner_id=owner.id,
        title=values.pop("title", key),
        description=values.pop("description", "deterministic problem statement"),
        idempotency_key=key,
        status=values.pop("status", "open"),
        classification_basis=values.pop("classification_basis", "manual"),
        authority=values.pop("authority", "founder"),
        **values,
    )


def test_problem_approach_component_and_assumption_history(superuser_db):
    owner = _owner(superuser_db)
    problem = _problem(superuser_db, owner)
    failed = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="overall weak approach",
        intended_outcome="would solve the issue",
        idempotency_key="attempt-a",
        status="proposed",
    )
    component = record_component(
        superuser_db,
        owner_id=owner.id,
        approach_id=failed.id,
        component_kind="race_condition_fix",
        description="use a fencing token",
        idempotency_key="component-a",
    )
    transition_approach(
        superuser_db,
        owner_id=owner.id,
        approach_id=failed.id,
        status="failed",
        reason="overall design violated source truth",
    )
    evaluation = evaluate_component(
        superuser_db,
        owner_id=owner.id,
        component_id=component.id,
        evaluation="verified_useful",
        reason="race regression passed",
        evaluator_basis="deterministic",
        idempotency_key="evaluation-a",
    )
    assumption = record_assumption(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        approach_id=failed.id,
        component_id=component.id,
        statement="leases never overlap",
        status="untested",
        idempotency_key="assumption-a",
    )
    transition_assumption(
        superuser_db,
        owner_id=owner.id,
        assumption_id=assumption.id,
        status="disproven",
        reason="stale-worker test",
    )

    assert failed.status == "failed"
    assert evaluation.evaluation == "verified_useful"
    assert useful_components_from_failed_approaches(
        superuser_db, owner_id=owner.id, problem_id=problem.id
    ) == [component]
    assert (
        unverified_assumptions(superuser_db, owner_id=owner.id, problem_id=problem.id)
        == []
    )
    assert assumption.status == "disproven"
    assert (
        len(
            approaches_for_problem(
                superuser_db, owner_id=owner.id, problem_id=problem.id
            )
        )
        == 1
    )


def test_observed_outcomes_decisions_and_problem_resolution_are_separate(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    problem = _problem(superuser_db, owner, mainai_task_id=task.id)
    approach = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="candidate",
        intended_outcome="success",
        idempotency_key="attempt",
    )
    outcome = record_outcome(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        approach_id=approach.id,
        outcome="failed",
        observation="deterministic test failed",
        deterministic=True,
        idempotency_key="observed",
    )
    ai = record_decision(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        decision="AI suggestion",
        authority="ai_interpretation",
        basis="ai_interpretation",
        status="unknown",
        idempotency_key="ai",
    )
    founder = record_decision(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        decision="Founder choice",
        authority="founder",
        basis="manual",
        status="active",
        chosen_approach_id=approach.id,
        idempotency_key="founder-1",
    )
    replacement = record_decision(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        decision="Corrected founder choice",
        authority="founder",
        basis="manual",
        status="active",
        supersedes_decision_id=founder.id,
        idempotency_key="founder-2",
    )
    task.status = "completed"
    task.completed_at = datetime.utcnow()
    superuser_db.flush()

    assert outcome.outcome == "failed" and approach.intended_outcome == "success"
    assert ai.authority == "ai_interpretation" and founder.authority == "founder"
    assert founder.status == "superseded"
    assert (
        active_decision(superuser_db, owner_id=owner.id, problem_id=problem.id)
        == replacement
    )
    assert (
        problem.status == "open"
    )  # task completion never resolves the problem implicitly
    transition_problem(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        status="partially_resolved",
        reason="one branch remains",
    )
    assert problem.status == "partially_resolved"
    assert problem in open_problems(superuser_db, owner_id=owner.id)


def test_cross_agent_evidence_lesson_reuse_and_multi_approach_synthesis(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    builder = record_execution(
        superuser_db,
        owner_id=owner.id,
        task_id=task.id,
        idempotency_key="builder",
        provider="vendor-a",
        model="builder",
        role="builder",
    )
    reviewer = record_execution(
        superuser_db,
        owner_id=owner.id,
        task_id=task.id,
        idempotency_key="reviewer",
        provider="vendor-b",
        model="reviewer",
        role="reviewer",
    )
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=builder.id,
        observer_execution_id=reviewer.id,
        evidence_kind="review",
        payload={"verified": True},
        source_type="deterministic_test",
        source_ref="run-1",
        review_kind="independent_model",
        deterministic=True,
        idempotency_key="evidence",
    )
    idea = record_idea(
        superuser_db,
        owner_id=owner.id,
        execution_id=builder.id,
        evidence_id=evidence.id,
        idea_kind="architectural_pattern",
        content="keep immutable evidence",
        disposition="accepted",
        disposition_reason="verified by review",
        idempotency_key="idea",
    )
    problem = _problem(superuser_db, owner, evidence_id=evidence.id)
    approach_a = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="A",
        execution_id=builder.id,
        idea_id=idea.id,
        idempotency_key="a",
    )
    approach_b = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="B",
        execution_id=reviewer.id,
        idempotency_key="b",
    )
    component_a = record_component(
        superuser_db,
        owner_id=owner.id,
        approach_id=approach_a.id,
        component_kind="architecture",
        description="immutable evidence",
        intelligence_idea_id=idea.id,
        idempotency_key="ca",
    )
    component_b = record_component(
        superuser_db,
        owner_id=owner.id,
        approach_id=approach_b.id,
        component_kind="test_strategy",
        description="negative verifier",
        idempotency_key="cb",
    )
    evaluation = evaluate_component(
        superuser_db,
        owner_id=owner.id,
        component_id=component_a.id,
        evaluation="verified_useful",
        reason="independent evidence",
        evaluator_execution_id=reviewer.id,
        evidence_id=evidence.id,
        idempotency_key="eval",
    )
    select_component(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        component_id=component_a.id,
    )
    select_component(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        component_id=component_b.id,
    )
    link_components(
        superuser_db,
        owner_id=owner.id,
        from_component_id=component_b.id,
        to_component_id=component_a.id,
        relation="supports",
        evidence_id=evidence.id,
    )
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active,
        problem="mutable evidence",
        root_cause="conflated interpretation",
        affected_component="problem_learning",
        severity=EngineeringLessonSeverity.high,
        evidence="test",
        fix="append-only observations",
        regression_test="test_problem_learning",
        general_rule="Never overwrite observations",
        source_type="test",
        source_ref="run-1",
        first_seen_at=datetime.utcnow(),
        confidence=EngineeringLessonConfidence.certain,
        created_by="test",
    )
    superuser_db.add(lesson)
    superuser_db.flush()
    link_engineering_lesson(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        engineering_lesson_id=lesson.id,
        relation="reusable_rule",
        approach_id=approach_a.id,
        component_id=component_a.id,
    )

    assert evaluation.evaluator_execution_id == reviewer.id
    assert component_a.intelligence_idea_id == idea.id
    assert superuser_db.query(LifeSolutionSelection).count() == 2
    assert superuser_db.query(EngineeringLesson).count() == 1
    assert (
        superuser_db.query(LifeProblemLessonLink).one().engineering_lesson_id
        == lesson.id
    )


def test_idempotency_invalid_and_cross_owner_references_fail_closed(superuser_db):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    problem_a = _problem(superuser_db, owner_a, "a")
    replay = _problem(superuser_db, owner_a, "a")
    assert replay.id == problem_a.id
    with pytest.raises(ProblemLearningError, match="idempotency"):
        _problem(superuser_db, owner_a, "a", title="changed")
    with pytest.raises(ProblemLearningError, match="another owner"):
        record_approach(
            superuser_db,
            owner_id=owner_b.id,
            problem_id=problem_a.id,
            description="cross owner",
            idempotency_key="cross",
        )
    problem_b = _problem(superuser_db, owner_b, "b")
    approach_b = record_approach(
        superuser_db,
        owner_id=owner_b.id,
        problem_id=problem_b.id,
        description="B",
        idempotency_key="b",
    )
    component_b = record_component(
        superuser_db,
        owner_id=owner_b.id,
        approach_id=approach_b.id,
        component_kind="unknown",
        description="B",
        idempotency_key="b",
    )
    with pytest.raises(ProblemLearningError, match="another owner"):
        select_component(
            superuser_db,
            owner_id=owner_a.id,
            problem_id=problem_a.id,
            component_id=component_b.id,
        )
    with pytest.raises(IntegrityError):
        superuser_db.execute(
            text(
                "INSERT INTO life_problem_approaches(id,owner_id,problem_id,description,idempotency_key) "
                "VALUES(gen_random_uuid(),:owner,:problem,'bad','bad')"
            ),
            {"owner": owner_b.id, "problem": problem_a.id},
        )
        superuser_db.flush()
    superuser_db.rollback()


def test_append_only_observations_cannot_be_rewritten(superuser_db):
    owner = _owner(superuser_db)
    problem = _problem(superuser_db, owner)
    approach = record_approach(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        description="attempt",
        idempotency_key="attempt",
    )
    component = record_component(
        superuser_db,
        owner_id=owner.id,
        approach_id=approach.id,
        component_kind="unknown",
        description="component",
        idempotency_key="component",
    )
    evaluation = evaluate_component(
        superuser_db,
        owner_id=owner.id,
        component_id=component.id,
        evaluation="unverified",
        reason="raw observation",
        idempotency_key="eval",
    )
    superuser_db.commit()
    evaluation.reason = "silently replaced"
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.commit()
    superuser_db.rollback()
    stored = superuser_db.get(LifeComponentEvaluation, evaluation.id)
    assert stored.reason == "raw observation"


def test_memory_thread_active_context_rls_and_provider_independence(
    superuser_db, db_session, monkeypatch
):
    owner_a, owner_b = _owner(superuser_db), _owner(superuser_db)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    thread = create_thread(
        superuser_db,
        owner_id=owner_a.id,
        idempotency_key="thread",
        manual_label="problem history",
        classification_basis="manual",
    )
    problem = _problem(superuser_db, owner_a, memory_thread_id=thread.id)
    add_member(
        superuser_db,
        owner_id=owner_a.id,
        thread_id=thread.id,
        member_kind="life_problem",
        member_ref_id=problem.id,
        membership_basis="explicit_reference",
    )
    context = create_context_set(
        superuser_db,
        owner_id=owner_a.id,
        anchor_type="life_problem",
        anchor_ref=str(problem.id),
        idempotency_key="context",
    )
    pin_object(
        superuser_db,
        owner_id=owner_a.id,
        context_set_id=context.id,
        object_type="life_problem",
        object_ref=str(problem.id),
    )
    original = problem.description
    superuser_db.commit()
    assert problem.description == original

    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_b.id)},
    )
    assert db_session.execute(select(LifeProblem)).scalars().all() == []
    db_session.rollback()
    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(owner_a.id)},
    )
    assert db_session.execute(select(LifeProblem)).scalar_one().id == problem.id


def test_database_rejects_multiple_active_decisions_and_preserves_events(superuser_db):
    owner = _owner(superuser_db)
    problem = _problem(superuser_db, owner)
    record_decision(
        superuser_db,
        owner_id=owner.id,
        problem_id=problem.id,
        decision="first",
        status="active",
        idempotency_key="first",
    )
    with pytest.raises(ProblemLearningError, match="explicitly supersede"):
        record_decision(
            superuser_db,
            owner_id=owner.id,
            problem_id=problem.id,
            decision="second",
            status="active",
            idempotency_key="second",
        )
    assert (
        superuser_db.execute(
            select(LifeProblemDecision).where(
                LifeProblemDecision.problem_id == problem.id
            )
        )
        .scalars()
        .all()
    )
    assert (
        superuser_db.execute(
            select(LifeProblemEvent).where(LifeProblemEvent.problem_id == problem.id)
        )
        .scalars()
        .all()
    )


def test_concurrent_problem_transition_is_serialized_and_audited_once(superuser_db):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    owner = _owner(superuser_db)
    problem = _problem(superuser_db, owner)
    owner_id, problem_id = owner.id, problem.id
    superuser_db.commit()

    def resolve(reason):
        db = sessionmaker(bind=migration_engine)()
        try:
            transition_problem(
                db,
                owner_id=owner_id,
                problem_id=problem_id,
                status="resolved",
                reason=reason,
            )
            db.commit()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(resolve, ["verifier one", "verifier two"]))
    events = (
        superuser_db.execute(
            select(LifeProblemEvent).where(
                LifeProblemEvent.problem_id == problem_id,
                LifeProblemEvent.event_type == "problem_status_changed",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
