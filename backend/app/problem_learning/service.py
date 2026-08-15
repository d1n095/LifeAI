"""Deterministic problem/solution/decision learning; no provider calls or inference."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.active_context.service import InvalidContextReference, _require_ref
from app.models.mainai_execution import EngineeringLesson
from app.models.problem_learning import (
    LifeApproachOutcome,
    LifeComponentEvaluation,
    LifeProblem,
    LifeProblemApproach,
    LifeProblemAssumption,
    LifeProblemDecision,
    LifeProblemEvent,
    LifeProblemLessonLink,
    LifeSolutionComponent,
    LifeSolutionComponentLink,
    LifeSolutionSelection,
)
from app.models.user import User


class ProblemLearningError(ValueError):
    pass


def _same(row, values):
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise ProblemLearningError(
            f"idempotency key reused with different fields: {', '.join(sorted(differing))}"
        )
    return row


def _problem(db, owner_id, problem_id, lock=False):
    q = select(LifeProblem).where(
        LifeProblem.id == problem_id, LifeProblem.owner_id == owner_id
    )
    row = db.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise ProblemLearningError("problem is missing or belongs to another owner")
    return row


def _owned(db, model, owner_id, row_id, label, lock=False):
    q = select(model).where(model.id == row_id, model.owner_id == owner_id)
    row = db.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise ProblemLearningError(f"{label} is missing or belongs to another owner")
    return row


def _event(db, problem, event_type, detail=None, actor="unknown"):
    db.add(
        LifeProblemEvent(
            owner_id=problem.owner_id,
            problem_id=problem.id,
            event_type=event_type,
            detail=detail or {},
            actor_type=actor,
        )
    )


def _validate_ref(db, owner_id, kind, value):
    if value is None:
        return
    try:
        _require_ref(db, owner_id, kind, value)
    except InvalidContextReference as exc:
        raise ProblemLearningError(str(exc)) from exc


def _approach_for_problem(db, owner_id, approach_id, problem_id, *, lock=False):
    approach = _owned(db, LifeProblemApproach, owner_id, approach_id, "approach", lock)
    if approach.problem_id != problem_id:
        raise ProblemLearningError("approach does not belong to problem")
    return approach


def _component_for_problem(db, owner_id, component_id, problem_id):
    component = _owned(db, LifeSolutionComponent, owner_id, component_id, "component")
    _approach_for_problem(db, owner_id, component.approach_id, problem_id)
    return component


def create_problem(
    db: Session,
    *,
    owner_id,
    title,
    description,
    idempotency_key,
    status="unknown",
    classification_basis="unknown",
    authority="unknown",
    provenance=None,
    memory_thread_id=None,
    life_intent_id=None,
    mainai_task_id=None,
    mainai_job_id=None,
    project_id=None,
    evidence_id=None,
    supersedes_problem_id=None,
):
    if (
        db.execute(
            select(User.id).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        raise ProblemLearningError("owner does not exist")
    values = dict(
        title=title,
        description=description,
        status=status,
        classification_basis=classification_basis,
        authority=authority,
        provenance=provenance or {},
        memory_thread_id=memory_thread_id,
        life_intent_id=life_intent_id,
        mainai_task_id=mainai_task_id,
        mainai_job_id=mainai_job_id,
        project_id=project_id,
        evidence_id=evidence_id,
        supersedes_problem_id=supersedes_problem_id,
    )
    existing = db.execute(
        select(LifeProblem).where(
            LifeProblem.owner_id == owner_id,
            LifeProblem.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    for kind, value in (
        ("memory_thread", memory_thread_id),
        ("life_intent", life_intent_id),
        ("mainai_task", mainai_task_id),
        ("mainai_job", mainai_job_id),
        ("project", project_id),
        ("intelligence_evidence", evidence_id),
    ):
        _validate_ref(db, owner_id, kind, value)
    if supersedes_problem_id:
        _problem(db, owner_id, supersedes_problem_id)
    row = LifeProblem(owner_id=owner_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    _event(
        db,
        row,
        "problem_created",
        {"status": status},
        "founder" if authority == "founder" else "system",
    )
    return row


def transition_problem(db, *, owner_id, problem_id, status, reason, actor="founder"):
    if not reason.strip():
        raise ProblemLearningError("problem transition requires a reason")
    row = _problem(db, owner_id, problem_id, True)
    old = row.status
    if old != status:
        row.status = status
        row.updated_at = datetime.utcnow()
        if status in {"resolved", "partially_resolved", "invalidated", "superseded"}:
            row.resolved_at = row.updated_at
        else:
            row.resolved_at = None
        _event(
            db,
            row,
            "problem_status_changed",
            {"old": old, "new": status, "reason": reason},
            actor,
        )
    return row


def record_approach(
    db,
    *,
    owner_id,
    problem_id,
    description,
    idempotency_key,
    status="unknown",
    basis="unknown",
    provenance=None,
    execution_id=None,
    idea_id=None,
    evidence_id=None,
    intended_outcome=None,
    rejection_reason=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    for kind, value in (
        ("intelligence_execution", execution_id),
        ("intelligence_idea", idea_id),
        ("intelligence_evidence", evidence_id),
    ):
        _validate_ref(db, owner_id, kind, value)
    values = dict(
        description=description,
        status=status,
        basis=basis,
        provenance=provenance or {},
        execution_id=execution_id,
        idea_id=idea_id,
        evidence_id=evidence_id,
        intended_outcome=intended_outcome,
        rejection_reason=rejection_reason,
    )
    existing = db.execute(
        select(LifeProblemApproach).where(
            LifeProblemApproach.owner_id == owner_id,
            LifeProblemApproach.problem_id == problem.id,
            LifeProblemApproach.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = LifeProblemApproach(
        owner_id=owner_id,
        problem_id=problem.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db, problem, "approach_created", {"approach_id": str(row.id), "status": status}
    )
    return row


def transition_approach(db, *, owner_id, approach_id, status, reason, actor="founder"):
    if not reason.strip():
        raise ProblemLearningError("approach transition requires a reason")
    row = _owned(db, LifeProblemApproach, owner_id, approach_id, "approach", True)
    old = row.status
    if old != status:
        row.status = status
        row.updated_at = datetime.utcnow()
        if status == "rejected":
            row.rejection_reason = reason
        _event(
            db,
            _problem(db, owner_id, row.problem_id),
            "approach_status_changed",
            {"approach_id": str(row.id), "old": old, "new": status, "reason": reason},
            actor,
        )
    return row


def record_component(
    db,
    *,
    owner_id,
    approach_id,
    component_kind,
    description,
    idempotency_key,
    basis="unknown",
    provenance=None,
    intelligence_idea_id=None,
):
    approach = _owned(db, LifeProblemApproach, owner_id, approach_id, "approach", True)
    _validate_ref(db, owner_id, "intelligence_idea", intelligence_idea_id)
    values = dict(
        component_kind=component_kind,
        description=description,
        basis=basis,
        provenance=provenance or {},
        intelligence_idea_id=intelligence_idea_id,
    )
    existing = db.execute(
        select(LifeSolutionComponent).where(
            LifeSolutionComponent.owner_id == owner_id,
            LifeSolutionComponent.approach_id == approach.id,
            LifeSolutionComponent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = LifeSolutionComponent(
        owner_id=owner_id,
        approach_id=approach.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        _problem(db, owner_id, approach.problem_id),
        "component_created",
        {"component_id": str(row.id), "approach_id": str(approach.id)},
    )
    return row


def evaluate_component(
    db,
    *,
    owner_id,
    component_id,
    evaluation,
    reason,
    idempotency_key,
    evaluator_basis="unknown",
    evidence_id=None,
    evaluator_execution_id=None,
    provenance=None,
):
    component = _owned(db, LifeSolutionComponent, owner_id, component_id, "component")
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    _validate_ref(db, owner_id, "intelligence_execution", evaluator_execution_id)
    values = dict(
        evaluation=evaluation,
        evaluator_basis=evaluator_basis,
        reason=reason,
        evidence_id=evidence_id,
        evaluator_execution_id=evaluator_execution_id,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(LifeComponentEvaluation).where(
            LifeComponentEvaluation.owner_id == owner_id,
            LifeComponentEvaluation.component_id == component.id,
            LifeComponentEvaluation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = LifeComponentEvaluation(
        owner_id=owner_id,
        component_id=component.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    return row


def record_assumption(
    db,
    *,
    owner_id,
    problem_id,
    statement,
    idempotency_key,
    status="unknown",
    basis="unknown",
    approach_id=None,
    component_id=None,
    evidence_id=None,
    provenance=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    if approach_id:
        _approach_for_problem(db, owner_id, approach_id, problem.id)
    if component_id:
        _component_for_problem(db, owner_id, component_id, problem.id)
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    values = dict(
        statement=statement,
        status=status,
        basis=basis,
        approach_id=approach_id,
        component_id=component_id,
        evidence_id=evidence_id,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(LifeProblemAssumption).where(
            LifeProblemAssumption.owner_id == owner_id,
            LifeProblemAssumption.problem_id == problem.id,
            LifeProblemAssumption.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = LifeProblemAssumption(
        owner_id=owner_id,
        problem_id=problem.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        problem,
        "assumption_created",
        {"assumption_id": str(row.id), "status": status},
    )
    return row


def transition_assumption(
    db,
    *,
    owner_id,
    assumption_id,
    status,
    reason,
    evidence_id=None,
    actor="deterministic_verifier",
):
    if not reason.strip():
        raise ProblemLearningError("assumption transition requires a reason")
    row = _owned(db, LifeProblemAssumption, owner_id, assumption_id, "assumption", True)
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    old = row.status
    if old != status:
        row.status = status
        row.updated_at = datetime.utcnow()
        row.evidence_id = evidence_id or row.evidence_id
        _event(
            db,
            _problem(db, owner_id, row.problem_id),
            "assumption_status_changed",
            {"assumption_id": str(row.id), "old": old, "new": status, "reason": reason},
            actor,
        )
    return row


def record_decision(
    db,
    *,
    owner_id,
    problem_id,
    decision,
    idempotency_key,
    status="unknown",
    authority="unknown",
    basis="unknown",
    alternatives=None,
    chosen_approach_id=None,
    chosen_component_id=None,
    supersedes_decision_id=None,
    provenance=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    if chosen_approach_id:
        _approach_for_problem(db, owner_id, chosen_approach_id, problem.id)
    if chosen_component_id:
        _component_for_problem(db, owner_id, chosen_component_id, problem.id)
    old = None
    if supersedes_decision_id:
        old = _owned(
            db, LifeProblemDecision, owner_id, supersedes_decision_id, "decision", True
        )
        if old.problem_id != problem.id:
            raise ProblemLearningError("superseded decision does not belong to problem")
    values = dict(
        decision=decision,
        status=status,
        authority=authority,
        basis=basis,
        alternatives=alternatives or [],
        chosen_approach_id=chosen_approach_id,
        chosen_component_id=chosen_component_id,
        supersedes_decision_id=supersedes_decision_id,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(LifeProblemDecision).where(
            LifeProblemDecision.owner_id == owner_id,
            LifeProblemDecision.problem_id == problem.id,
            LifeProblemDecision.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    if status == "active" and old is None:
        current = active_decision(db, owner_id=owner_id, problem_id=problem.id)
        if current is not None:
            raise ProblemLearningError(
                "an active decision already exists; explicitly supersede it"
            )
    if old and old.status == "active":
        old.status = "superseded"
        old.updated_at = datetime.utcnow()
    row = LifeProblemDecision(
        owner_id=owner_id,
        problem_id=problem.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        problem,
        "decision_created",
        {
            "decision_id": str(row.id),
            "authority": authority,
            "supersedes": str(supersedes_decision_id)
            if supersedes_decision_id
            else None,
        },
        "founder"
        if authority == "founder"
        else "ai_interpretation"
        if authority == "ai_interpretation"
        else "system",
    )
    return row


def record_outcome(
    db,
    *,
    owner_id,
    problem_id,
    approach_id,
    outcome,
    observation,
    idempotency_key,
    evidence_id=None,
    deterministic=False,
    provenance=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    approach = _owned(db, LifeProblemApproach, owner_id, approach_id, "approach")
    if approach.problem_id != problem.id:
        raise ProblemLearningError("approach does not belong to problem")
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    values = dict(
        problem_id=problem.id,
        outcome=outcome,
        observation=observation,
        evidence_id=evidence_id,
        deterministic=deterministic,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(LifeApproachOutcome).where(
            LifeApproachOutcome.owner_id == owner_id,
            LifeApproachOutcome.approach_id == approach.id,
            LifeApproachOutcome.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = LifeApproachOutcome(
        owner_id=owner_id,
        approach_id=approach.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db, problem, "outcome_recorded", {"outcome_id": str(row.id), "outcome": outcome}
    )
    return row


def link_engineering_lesson(
    db,
    *,
    owner_id,
    problem_id,
    engineering_lesson_id,
    relation,
    approach_id=None,
    component_id=None,
    outcome_id=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    if db.get(EngineeringLesson, engineering_lesson_id) is None:
        raise ProblemLearningError("engineering lesson does not exist")
    if approach_id:
        _approach_for_problem(db, owner_id, approach_id, problem.id)
    if component_id:
        _component_for_problem(db, owner_id, component_id, problem.id)
    if outcome_id:
        outcome = _owned(db, LifeApproachOutcome, owner_id, outcome_id, "outcome")
        if outcome.problem_id != problem.id:
            raise ProblemLearningError("outcome does not belong to problem")
    row = db.get(LifeProblemLessonLink, (owner_id, problem.id, engineering_lesson_id))
    values = dict(
        approach_id=approach_id,
        component_id=component_id,
        outcome_id=outcome_id,
        relation=relation,
    )
    if row:
        return _same(row, values)
    row = LifeProblemLessonLink(
        owner_id=owner_id,
        problem_id=problem.id,
        engineering_lesson_id=engineering_lesson_id,
        **values,
    )
    db.add(row)
    db.flush()
    _event(
        db,
        problem,
        "lesson_linked",
        {"engineering_lesson_id": str(engineering_lesson_id), "relation": relation},
    )
    return row


def select_component(
    db,
    *,
    owner_id,
    problem_id,
    component_id,
    role="unknown",
    basis="unknown",
    evidence_id=None,
):
    problem = _problem(db, owner_id, problem_id, True)
    component = _component_for_problem(db, owner_id, component_id, problem.id)
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    row = db.get(LifeSolutionSelection, (owner_id, problem.id, component.id))
    values = dict(role=role, basis=basis, evidence_id=evidence_id)
    if row:
        return _same(row, values)
    row = LifeSolutionSelection(
        owner_id=owner_id, problem_id=problem.id, component_id=component.id, **values
    )
    db.add(row)
    db.flush()
    _event(
        db,
        problem,
        "component_selected",
        {"component_id": str(component.id), "role": role},
    )
    return row


def link_components(
    db, *, owner_id, from_component_id, to_component_id, relation, evidence_id=None
):
    if from_component_id == to_component_id:
        raise ProblemLearningError("component self-link is invalid")
    first, second = sorted((from_component_id, to_component_id), key=str)
    rows = list(
        db.execute(
            select(LifeSolutionComponent)
            .where(
                LifeSolutionComponent.owner_id == owner_id,
                LifeSolutionComponent.id.in_([first, second]),
            )
            .order_by(LifeSolutionComponent.id)
            .with_for_update()
        ).scalars()
    )
    if len(rows) != 2:
        raise ProblemLearningError("component link crosses owner or missing component")
    problem_ids = {
        _owned(
            db, LifeProblemApproach, owner_id, row.approach_id, "approach"
        ).problem_id
        for row in rows
    }
    if len(problem_ids) != 1:
        raise ProblemLearningError("linked components must address the same problem")
    _validate_ref(db, owner_id, "intelligence_evidence", evidence_id)
    row = db.execute(
        select(LifeSolutionComponentLink).where(
            LifeSolutionComponentLink.owner_id == owner_id,
            LifeSolutionComponentLink.from_component_id == from_component_id,
            LifeSolutionComponentLink.to_component_id == to_component_id,
            LifeSolutionComponentLink.relation == relation,
        )
    ).scalar_one_or_none()
    if row:
        return _same(row, {"evidence_id": evidence_id})
    row = LifeSolutionComponentLink(
        owner_id=owner_id,
        from_component_id=from_component_id,
        to_component_id=to_component_id,
        relation=relation,
        evidence_id=evidence_id,
    )
    db.add(row)
    db.flush()
    return row


def open_problems(db, *, owner_id):
    return list(
        db.execute(
            select(LifeProblem)
            .where(
                LifeProblem.owner_id == owner_id,
                LifeProblem.status.in_(
                    [
                        "open",
                        "investigating",
                        "blocked",
                        "partially_resolved",
                        "unknown",
                    ]
                ),
            )
            .order_by(LifeProblem.created_at)
        ).scalars()
    )


def approaches_for_problem(db, *, owner_id, problem_id, status=None):
    _problem(db, owner_id, problem_id)
    q = select(LifeProblemApproach).where(
        LifeProblemApproach.owner_id == owner_id,
        LifeProblemApproach.problem_id == problem_id,
    )
    if status:
        q = q.where(LifeProblemApproach.status == status)
    return list(db.execute(q.order_by(LifeProblemApproach.created_at)).scalars())


def unverified_assumptions(db, *, owner_id, problem_id):
    _problem(db, owner_id, problem_id)
    return list(
        db.execute(
            select(LifeProblemAssumption).where(
                LifeProblemAssumption.owner_id == owner_id,
                LifeProblemAssumption.problem_id == problem_id,
                LifeProblemAssumption.status.in_(["untested", "unknown"]),
            )
        ).scalars()
    )


def active_decision(db, *, owner_id, problem_id):
    _problem(db, owner_id, problem_id)
    return (
        db.execute(
            select(LifeProblemDecision)
            .where(
                LifeProblemDecision.owner_id == owner_id,
                LifeProblemDecision.problem_id == problem_id,
                LifeProblemDecision.status == "active",
            )
            .order_by(LifeProblemDecision.decided_at.desc())
        )
        .scalars()
        .first()
    )


def useful_components_from_failed_approaches(db, *, owner_id, problem_id):
    return list(
        db.execute(
            select(LifeSolutionComponent)
            .join(
                LifeProblemApproach,
                LifeSolutionComponent.approach_id == LifeProblemApproach.id,
            )
            .join(
                LifeComponentEvaluation,
                LifeComponentEvaluation.component_id == LifeSolutionComponent.id,
            )
            .where(
                LifeSolutionComponent.owner_id == owner_id,
                LifeProblemApproach.problem_id == problem_id,
                LifeProblemApproach.status.in_(["failed", "rejected"]),
                LifeComponentEvaluation.evaluation.in_(
                    ["verified_useful", "useful_with_changes"]
                ),
            )
        )
        .scalars()
        .unique()
    )
