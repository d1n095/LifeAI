"""Provider-free strategy comparison, experiment and promotion services."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.models.intelligence_governance import IntelligenceEvidence, IntelligenceIdea
from app.models.problem_learning import LifeProblemAssumption, LifeSolutionComponent
from app.models.strategy_evaluation import (
    StrategyComparabilityAssessment,
    StrategyComparison,
    StrategyEfficiencyDelta,
    StrategyEvaluationEvent,
    StrategyExperiment,
    StrategyExperimentComparison,
    StrategyLearningLink,
    StrategyLearningObservation,
    StrategyPromotionCandidate,
    StrategyPromotionComparison,
    StrategyQualityAssessment,
)
from app.models.work_intelligence import (
    WorkEfficiencyObservation,
    WorkStoppingDecision,
    WorkStrategy,
    WorkStrategyExecution,
    WorkTraceEvent,
)
from app.work_intelligence.service import verification_state


class StrategyEvaluationError(ValueError):
    pass


def _same(row, values):
    differences = [key for key, value in values.items() if getattr(row, key) != value]
    if differences:
        raise StrategyEvaluationError(
            f"idempotency key reused with different fields: {', '.join(sorted(differences))}"
        )
    return row


def _owned(db, model, owner_id, row_id, label, *, lock=False):
    query = select(model).where(model.id == row_id, model.owner_id == owner_id)
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise StrategyEvaluationError(f"{label} is missing or belongs to another owner")
    return row


def _idempotent(db, model, owner_id, key, values):
    existing = db.execute(
        select(model).where(model.owner_id == owner_id, model.idempotency_key == key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = model(owner_id=owner_id, idempotency_key=key, **values)
    db.add(row)
    db.flush()
    return row


def create_comparison(
    db,
    *,
    owner_id,
    baseline_binding_id,
    challenger_binding_id,
    idempotency_key,
    problem_id=None,
    task_id=None,
    task_type=None,
    domain=None,
    risk_level="unknown",
    comparison_basis="unknown",
    provenance=None,
):
    baseline = _owned(
        db, WorkStrategyExecution, owner_id, baseline_binding_id, "baseline binding"
    )
    challenger = _owned(
        db, WorkStrategyExecution, owner_id, challenger_binding_id, "challenger binding"
    )
    if baseline.id == challenger.id:
        raise StrategyEvaluationError("baseline and challenger must be distinct")
    for binding in (baseline, challenger):
        if problem_id is not None and binding.problem_id not in (None, problem_id):
            raise StrategyEvaluationError(
                "comparison problem conflicts with an execution binding"
            )
    values = dict(
        baseline_binding_id=baseline.id,
        challenger_binding_id=challenger.id,
        problem_id=problem_id,
        task_id=task_id,
        task_type=task_type,
        domain=domain,
        risk_level=risk_level,
        comparison_basis=comparison_basis,
        provenance=provenance or {},
    )
    return _idempotent(db, StrategyComparison, owner_id, idempotency_key, values)


def assess_comparability(
    db,
    *,
    owner_id,
    comparison_id,
    status,
    dimensions,
    reasons,
    idempotency_key,
    basis="unknown",
    evidence_id=None,
    supersedes_id=None,
):
    comparison = _owned(
        db, StrategyComparison, owner_id, comparison_id, "comparison", lock=True
    )
    evidence = (
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
        if evidence_id
        else None
    )
    supersedes = (
        _owned(
            db,
            StrategyComparabilityAssessment,
            owner_id,
            supersedes_id,
            "prior assessment",
        )
        if supersedes_id
        else None
    )
    if supersedes and supersedes.comparison_id != comparison.id:
        raise StrategyEvaluationError(
            "superseded assessment belongs to another comparison"
        )
    values = dict(
        comparison_id=comparison.id,
        status=status,
        dimensions=dimensions or {},
        reasons=reasons or [],
        basis=basis,
        evidence_id=evidence.id if evidence else None,
        supersedes_id=supersedes.id if supersedes else None,
    )
    return _idempotent(
        db, StrategyComparabilityAssessment, owner_id, idempotency_key, values
    )


def assess_quality(
    db,
    *,
    owner_id,
    comparison_id,
    subject,
    idempotency_key,
    reason,
    unresolved_regression=False,
    scope_violation=False,
    evidence_id=None,
):
    comparison = _owned(db, StrategyComparison, owner_id, comparison_id, "comparison")
    if subject not in {"baseline", "challenger"}:
        raise StrategyEvaluationError("subject must be baseline or challenger")
    binding_id = (
        comparison.baseline_binding_id
        if subject == "baseline"
        else comparison.challenger_binding_id
    )
    verification = verification_state(
        db, owner_id=owner_id, strategy_execution_id=binding_id
    )
    evidence = (
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
        if evidence_id
        else None
    )
    if scope_violation:
        state = "invalid_comparison"
    elif unresolved_regression:
        state = "regression_detected"
    elif verification.failed:
        state = "quality_fail"
    elif not verification.obligations_satisfied:
        state = "verification_incomplete"
    else:
        state = "quality_pass"
    values = dict(
        comparison_id=comparison.id,
        subject=subject,
        state=state,
        required_count=verification.required,
        passed_count=verification.passed,
        failed_count=verification.failed,
        missing_count=verification.missing,
        unresolved_regression=unresolved_regression,
        scope_violation=scope_violation,
        reason=reason,
        evidence_id=evidence.id if evidence else None,
    )
    return _idempotent(db, StrategyQualityAssessment, owner_id, idempotency_key, values)


def record_efficiency_delta(
    db,
    *,
    owner_id,
    comparison_id,
    baseline_observation_id,
    challenger_observation_id,
    idempotency_key,
):
    comparison = _owned(db, StrategyComparison, owner_id, comparison_id, "comparison")
    baseline = _owned(
        db,
        WorkEfficiencyObservation,
        owner_id,
        baseline_observation_id,
        "baseline observation",
    )
    challenger = _owned(
        db,
        WorkEfficiencyObservation,
        owner_id,
        challenger_observation_id,
        "challenger observation",
    )
    if (
        baseline.strategy_execution_id != comparison.baseline_binding_id
        or challenger.strategy_execution_id != comparison.challenger_binding_id
    ):
        raise StrategyEvaluationError(
            "efficiency observations do not match comparison sides"
        )
    if (
        baseline.metric_type != challenger.metric_type
        or baseline.unit != challenger.unit
    ):
        raise StrategyEvaluationError(
            "efficiency observations must use the same metric and unit"
        )
    values = dict(
        comparison_id=comparison.id,
        baseline_observation_id=baseline.id,
        challenger_observation_id=challenger.id,
        metric_type=baseline.metric_type,
        unit=baseline.unit,
        baseline_value=baseline.numeric_value,
        challenger_value=challenger.numeric_value,
        delta_value=challenger.numeric_value - baseline.numeric_value,
    )
    return _idempotent(db, StrategyEfficiencyDelta, owner_id, idempotency_key, values)


def create_experiment(
    db,
    *,
    owner_id,
    baseline_strategy_id,
    challenger_strategy_id,
    hypothesis,
    intended_change,
    expected_benefit,
    idempotency_key,
    quality_invariants=None,
    scope=None,
    applicability=None,
    required_sample_count=None,
    provenance=None,
):
    baseline = _owned(
        db, WorkStrategy, owner_id, baseline_strategy_id, "baseline strategy"
    )
    challenger = _owned(
        db, WorkStrategy, owner_id, challenger_strategy_id, "challenger strategy"
    )
    values = dict(
        baseline_strategy_id=baseline.id,
        challenger_strategy_id=challenger.id,
        hypothesis=hypothesis,
        intended_change=intended_change,
        expected_benefit=expected_benefit,
        quality_invariants=quality_invariants or [],
        scope=scope or {},
        applicability=applicability or {},
        required_sample_count=required_sample_count,
        provenance=provenance or {},
        state="draft",
        failure_reason=None,
    )
    row = _idempotent(db, StrategyExperiment, owner_id, idempotency_key, values)
    _record_event(
        db,
        owner_id=owner_id,
        experiment_id=row.id,
        event_type="created",
        idempotency_key=f"{idempotency_key}:created",
        to_state="draft",
    )
    return row


EXPERIMENT_TRANSITIONS = {
    "draft": {"ready", "cancelled", "invalidated"},
    "ready": {"running", "cancelled", "invalidated"},
    "running": {"completed", "failed", "cancelled", "invalidated"},
    "completed": {"invalidated"},
    "failed": {"invalidated"},
    "cancelled": set(),
    "invalidated": set(),
}

CANDIDATE_TRANSITIONS = {
    "candidate": {"insufficient_evidence", "under_review", "rejected", "invalidated"},
    "insufficient_evidence": {"under_review", "rejected", "invalidated"},
    "under_review": {"approved", "rejected", "insufficient_evidence", "invalidated"},
    "approved": {"superseded", "invalidated"},
    "rejected": set(),
    "superseded": set(),
    "invalidated": set(),
}


def _record_event(
    db,
    *,
    owner_id,
    idempotency_key,
    event_type,
    experiment_id=None,
    candidate_id=None,
    from_state=None,
    to_state=None,
    detail=None,
):
    return _idempotent(
        db,
        StrategyEvaluationEvent,
        owner_id,
        idempotency_key,
        dict(
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            detail=detail or {},
        ),
    )


def transition_experiment(
    db, *, owner_id, experiment_id, to_state, idempotency_key, failure_reason=None
):
    row = _owned(
        db, StrategyExperiment, owner_id, experiment_id, "experiment", lock=True
    )
    if to_state not in EXPERIMENT_TRANSITIONS[row.state]:
        raise StrategyEvaluationError(
            f"invalid experiment transition {row.state} -> {to_state}"
        )
    old = row.state
    row.state, row.failure_reason = to_state, failure_reason
    event = _record_event(
        db,
        owner_id=owner_id,
        experiment_id=row.id,
        event_type="state_changed",
        idempotency_key=idempotency_key,
        from_state=old,
        to_state=to_state,
        detail={"failure_reason": failure_reason} if failure_reason else {},
    )
    db.flush()
    return row, event


def create_promotion_candidate(
    db,
    *,
    owner_id,
    strategy_id,
    baseline_strategy_id,
    idempotency_key,
    applicable_context=None,
    known_tradeoffs=None,
    confidence_basis="unknown",
    minimum_valid_comparisons=None,
    provenance=None,
):
    strategy = _owned(db, WorkStrategy, owner_id, strategy_id, "strategy")
    baseline = _owned(
        db, WorkStrategy, owner_id, baseline_strategy_id, "baseline strategy"
    )
    values = dict(
        strategy_id=strategy.id,
        baseline_strategy_id=baseline.id,
        applicable_context=applicable_context or {},
        known_tradeoffs=known_tradeoffs or [],
        confidence_basis=confidence_basis,
        minimum_valid_comparisons=minimum_valid_comparisons,
        state="candidate",
        provenance=provenance or {},
    )
    row = _idempotent(db, StrategyPromotionCandidate, owner_id, idempotency_key, values)
    _record_event(
        db,
        owner_id=owner_id,
        candidate_id=row.id,
        event_type="created",
        idempotency_key=f"{idempotency_key}:created",
        to_state="candidate",
    )
    return row


def link_comparison(
    db,
    *,
    owner_id,
    comparison_id,
    idempotency_key,
    experiment_id=None,
    candidate_id=None,
):
    comparison = _owned(db, StrategyComparison, owner_id, comparison_id, "comparison")
    if (experiment_id is None) == (candidate_id is None):
        raise StrategyEvaluationError("exactly one experiment or candidate is required")
    if experiment_id:
        parent = _owned(db, StrategyExperiment, owner_id, experiment_id, "experiment")
        model, parent_field = StrategyExperimentComparison, "experiment_id"
        if (
            parent.baseline_strategy_id
            != _owned(
                db,
                WorkStrategyExecution,
                owner_id,
                comparison.baseline_binding_id,
                "baseline binding",
            ).strategy_id
            or parent.challenger_strategy_id
            != _owned(
                db,
                WorkStrategyExecution,
                owner_id,
                comparison.challenger_binding_id,
                "challenger binding",
            ).strategy_id
        ):
            raise StrategyEvaluationError(
                "comparison strategies do not match experiment"
            )
    else:
        parent = _owned(
            db, StrategyPromotionCandidate, owner_id, candidate_id, "candidate"
        )
        model, parent_field = StrategyPromotionComparison, "candidate_id"
        if (
            parent.baseline_strategy_id
            != _owned(
                db,
                WorkStrategyExecution,
                owner_id,
                comparison.baseline_binding_id,
                "baseline binding",
            ).strategy_id
            or parent.strategy_id
            != _owned(
                db,
                WorkStrategyExecution,
                owner_id,
                comparison.challenger_binding_id,
                "challenger binding",
            ).strategy_id
        ):
            raise StrategyEvaluationError(
                "comparison strategies do not match candidate"
            )
    link = _idempotent(
        db,
        model,
        owner_id,
        idempotency_key,
        {parent_field: parent.id, "comparison_id": comparison.id},
    )
    _record_event(
        db,
        owner_id=owner_id,
        experiment_id=parent.id if experiment_id else None,
        candidate_id=parent.id if candidate_id else None,
        event_type="comparison_linked",
        idempotency_key=f"{idempotency_key}:event",
        detail={"comparison_id": str(comparison.id)},
    )
    return link


@dataclass(frozen=True)
class PromotionEvidenceSummary:
    comparison_count: int
    valid_comparison_count: int
    invalid_comparison_count: int
    quality_pass_count: int
    quality_fail_count: int
    unresolved_conflicts: int
    promotable: bool


def promotion_summary(db, *, owner_id, candidate_id):
    candidate = _owned(
        db, StrategyPromotionCandidate, owner_id, candidate_id, "candidate"
    )
    links = list(
        db.execute(
            select(StrategyPromotionComparison).where(
                StrategyPromotionComparison.owner_id == owner_id,
                StrategyPromotionComparison.candidate_id == candidate.id,
            )
        ).scalars()
    )
    valid = invalid = passed = failed = conflicts = 0
    for link in links:
        comparability = (
            db.execute(
                select(StrategyComparabilityAssessment)
                .where(
                    StrategyComparabilityAssessment.owner_id == owner_id,
                    StrategyComparabilityAssessment.comparison_id == link.comparison_id,
                )
                .order_by(
                    StrategyComparabilityAssessment.created_at.desc(),
                    StrategyComparabilityAssessment.id.desc(),
                )
            )
            .scalars()
            .first()
        )
        quality = (
            db.execute(
                select(StrategyQualityAssessment)
                .where(
                    StrategyQualityAssessment.owner_id == owner_id,
                    StrategyQualityAssessment.comparison_id == link.comparison_id,
                    StrategyQualityAssessment.subject == "challenger",
                )
                .order_by(
                    StrategyQualityAssessment.created_at.desc(),
                    StrategyQualityAssessment.id.desc(),
                )
            )
            .scalars()
            .first()
        )
        if not comparability or comparability.status not in {
            "comparable",
            "partially_comparable",
        }:
            invalid += 1
            continue
        valid += 1
        if quality and quality.state == "quality_pass":
            passed += 1
        else:
            failed += 1
        if quality and (quality.unresolved_regression or quality.scope_violation):
            conflicts += 1
    minimum = candidate.minimum_valid_comparisons or 2
    return PromotionEvidenceSummary(
        len(links),
        valid,
        invalid,
        passed,
        failed,
        conflicts,
        valid >= minimum and passed == valid and failed == 0 and conflicts == 0,
    )


def transition_candidate(db, *, owner_id, candidate_id, to_state, idempotency_key):
    row = _owned(
        db, StrategyPromotionCandidate, owner_id, candidate_id, "candidate", lock=True
    )
    if to_state not in CANDIDATE_TRANSITIONS[row.state]:
        raise StrategyEvaluationError(
            f"invalid candidate transition {row.state} -> {to_state}"
        )
    if (
        to_state == "approved"
        and not promotion_summary(db, owner_id=owner_id, candidate_id=row.id).promotable
    ):
        raise StrategyEvaluationError(
            "quality-safe evidence threshold is not satisfied"
        )
    old, row.state = row.state, to_state
    event = _record_event(
        db,
        owner_id=owner_id,
        candidate_id=row.id,
        event_type="state_changed",
        idempotency_key=idempotency_key,
        from_state=old,
        to_state=to_state,
    )
    db.flush()
    return row, event


def link_learning_subject(
    db,
    *,
    owner_id,
    idempotency_key,
    disposition,
    relation,
    reason,
    comparison_id=None,
    experiment_id=None,
    idea_id=None,
    component_id=None,
    assumption_id=None,
    evidence_id=None,
):
    if (
        sum(value is not None for value in (comparison_id, experiment_id)) != 1
        or sum(value is not None for value in (idea_id, component_id, assumption_id))
        != 1
    ):
        raise StrategyEvaluationError(
            "exactly one parent and one learning subject are required"
        )
    if comparison_id:
        _owned(db, StrategyComparison, owner_id, comparison_id, "comparison")
    if experiment_id:
        _owned(db, StrategyExperiment, owner_id, experiment_id, "experiment")
    if idea_id:
        _owned(db, IntelligenceIdea, owner_id, idea_id, "idea")
    if component_id:
        _owned(db, LifeSolutionComponent, owner_id, component_id, "component")
    if assumption_id:
        _owned(db, LifeProblemAssumption, owner_id, assumption_id, "assumption")
    if evidence_id:
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
    return _idempotent(
        db,
        StrategyLearningLink,
        owner_id,
        idempotency_key,
        dict(
            comparison_id=comparison_id,
            experiment_id=experiment_id,
            idea_id=idea_id,
            component_id=component_id,
            assumption_id=assumption_id,
            disposition=disposition,
            relation=relation,
            evidence_id=evidence_id,
            reason=reason,
        ),
    )


def record_learning_observation(
    db,
    *,
    owner_id,
    comparison_id,
    observation_type,
    reason,
    idempotency_key,
    trace_event_id=None,
    related_trace_event_id=None,
    stopping_decision_id=None,
    numeric_value=None,
    unit=None,
    basis="unknown",
):
    comparison = _owned(
        db, StrategyComparison, owner_id, comparison_id, "comparison", lock=True
    )
    allowed_bindings = {
        comparison.baseline_binding_id,
        comparison.challenger_binding_id,
    }
    for model, row_id, label in (
        (WorkTraceEvent, trace_event_id, "trace event"),
        (WorkTraceEvent, related_trace_event_id, "related trace event"),
        (WorkStoppingDecision, stopping_decision_id, "stopping decision"),
    ):
        if row_id:
            row = _owned(db, model, owner_id, row_id, label)
            if row.strategy_execution_id not in allowed_bindings:
                raise StrategyEvaluationError(
                    f"{label} does not belong to the comparison"
                )
    return _idempotent(
        db,
        StrategyLearningObservation,
        owner_id,
        idempotency_key,
        dict(
            comparison_id=comparison.id,
            observation_type=observation_type,
            trace_event_id=trace_event_id,
            related_trace_event_id=related_trace_event_id,
            stopping_decision_id=stopping_decision_id,
            numeric_value=Decimal(str(numeric_value))
            if numeric_value is not None
            else None,
            unit=unit,
            reason=reason,
            basis=basis,
        ),
    )
