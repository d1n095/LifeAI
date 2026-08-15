"""Deterministic recording/query services; never routes work or invokes a provider."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.active_context.service import InvalidContextReference, _require_ref
from app.models.intelligence_governance import IntelligenceExecution
from app.models.mainai_execution import EngineeringLesson
from app.models.usage import UsageLog
from app.models.user import User
from app.models.work_intelligence import (
    WorkEfficiencyObservation,
    WorkSpecialistContribution,
    WorkStoppingDecision,
    WorkStrategy,
    WorkStrategyExecution,
    WorkStrategyFinding,
    WorkStrategyLessonLink,
    WorkTraceEvent,
    WorkVerificationObligation,
    WorkVerificationObservation,
)


class WorkIntelligenceError(ValueError):
    pass


def _same(row, values):
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise WorkIntelligenceError(
            f"idempotency key reused with different fields: {', '.join(sorted(differing))}"
        )
    return row


def _owned(db, model, owner_id, row_id, label, *, lock=False):
    query = select(model).where(model.id == row_id, model.owner_id == owner_id)
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise WorkIntelligenceError(f"{label} is missing or belongs to another owner")
    return row


def _ref(db, owner_id, kind, value):
    if value is None:
        return None
    try:
        return _require_ref(db, owner_id, kind, value)[1]
    except InvalidContextReference as exc:
        raise WorkIntelligenceError(str(exc)) from exc


def create_strategy(
    db,
    *,
    owner_id,
    strategy_key,
    version,
    idempotency_key,
    work_category="unknown",
    ordered_phases=None,
    tool_sequence=None,
    methods=None,
    environment_assumptions=None,
    classification_basis="unknown",
    provenance=None,
    predecessor_id=None,
):
    if (
        db.execute(
            select(User.id).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        is None
    ):
        raise WorkIntelligenceError("owner does not exist")
    predecessor = None
    if predecessor_id:
        predecessor = _owned(
            db, WorkStrategy, owner_id, predecessor_id, "predecessor strategy"
        )
        if predecessor.strategy_key != strategy_key or predecessor.version >= version:
            raise WorkIntelligenceError(
                "predecessor must be an earlier version of the same strategy"
            )
    values = dict(
        strategy_key=strategy_key,
        version=version,
        work_category=work_category,
        ordered_phases=ordered_phases or [],
        tool_sequence=tool_sequence or [],
        methods=methods or {},
        environment_assumptions=environment_assumptions or {},
        classification_basis=classification_basis,
        provenance=provenance or {},
        predecessor_id=predecessor.id if predecessor else None,
    )
    existing = db.execute(
        select(WorkStrategy).where(
            WorkStrategy.owner_id == owner_id,
            WorkStrategy.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = WorkStrategy(owner_id=owner_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def bind_strategy_execution(
    db,
    *,
    owner_id,
    strategy_id,
    execution_id,
    idempotency_key,
    problem_id=None,
    approach_id=None,
    basis="unknown",
    provenance=None,
):
    strategy = _owned(db, WorkStrategy, owner_id, strategy_id, "strategy")
    execution = _owned(
        db, IntelligenceExecution, owner_id, execution_id, "execution", lock=True
    )
    problem = _ref(db, owner_id, "life_problem", problem_id)
    approach = _ref(db, owner_id, "life_problem_approach", approach_id)
    if approach and (problem is None or approach.problem_id != problem.id):
        raise WorkIntelligenceError("approach requires its owning problem")
    values = dict(
        strategy_id=strategy.id,
        execution_id=execution.id,
        problem_id=problem.id if problem else None,
        approach_id=approach.id if approach else None,
        basis=basis,
        provenance=provenance or {},
    )
    existing = db.execute(
        select(WorkStrategyExecution).where(
            WorkStrategyExecution.owner_id == owner_id,
            WorkStrategyExecution.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = WorkStrategyExecution(
        owner_id=owner_id, idempotency_key=idempotency_key, **values
    )
    db.add(row)
    db.flush()
    return row


def record_trace_event(
    db,
    *,
    owner_id,
    strategy_execution_id,
    action_type,
    idempotency_key,
    tool_identity=None,
    target_type=None,
    target_ref=None,
    action_detail=None,
    result="unknown",
    duration_ms=None,
    items_count=None,
    bytes_count=None,
    evidence_id=None,
    usage_log_id=None,
    basis="unknown",
    provenance=None,
    occurred_at=None,
):
    binding = _owned(
        db,
        WorkStrategyExecution,
        owner_id,
        strategy_execution_id,
        "strategy execution",
        lock=True,
    )
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    usage = db.get(UsageLog, usage_log_id) if usage_log_id else None
    if usage_log_id and (usage is None or usage.user_id != owner_id):
        raise WorkIntelligenceError("usage log is missing or belongs to another owner")
    values = dict(
        action_type=action_type,
        tool_identity=tool_identity,
        target_type=target_type,
        target_ref=target_ref,
        action_detail=action_detail or {},
        result=result,
        duration_ms=duration_ms,
        items_count=items_count,
        bytes_count=bytes_count,
        evidence_id=evidence.id if evidence else None,
        usage_log_id=usage.id if usage else None,
        basis=basis,
        provenance=provenance or {},
    )
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    existing = db.execute(
        select(WorkTraceEvent).where(
            WorkTraceEvent.owner_id == owner_id,
            WorkTraceEvent.strategy_execution_id == binding.id,
            WorkTraceEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        comparison = dict(values)
        if occurred_at is None:
            comparison.pop("occurred_at", None)
        return _same(existing, comparison)
    row = WorkTraceEvent(
        owner_id=owner_id,
        strategy_execution_id=binding.id,
        sequence_number=binding.next_trace_sequence,
        idempotency_key=idempotency_key,
        **values,
    )
    binding.next_trace_sequence += 1
    db.add(row)
    db.flush()
    return row


def _record_for_execution(
    db, model, *, owner_id, strategy_execution_id, idempotency_key, values
):
    binding = _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    existing = db.execute(
        select(model).where(
            model.owner_id == owner_id,
            model.strategy_execution_id == binding.id,
            model.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = model(
        owner_id=owner_id,
        strategy_execution_id=binding.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    return row


def record_efficiency_observation(
    db,
    *,
    owner_id,
    strategy_execution_id,
    metric_type,
    numeric_value,
    unit,
    idempotency_key,
    trace_event_id=None,
    evidence_id=None,
    basis="unknown",
    provenance=None,
):
    trace = (
        _owned(db, WorkTraceEvent, owner_id, trace_event_id, "trace event")
        if trace_event_id
        else None
    )
    if trace and trace.strategy_execution_id != strategy_execution_id:
        raise WorkIntelligenceError("trace event does not belong to strategy execution")
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    return _record_for_execution(
        db,
        WorkEfficiencyObservation,
        owner_id=owner_id,
        strategy_execution_id=strategy_execution_id,
        idempotency_key=idempotency_key,
        values=dict(
            metric_type=metric_type,
            numeric_value=Decimal(str(numeric_value)),
            unit=unit,
            trace_event_id=trace.id if trace else None,
            evidence_id=evidence.id if evidence else None,
            basis=basis,
            provenance=provenance or {},
        ),
    )


def record_strategy_finding(
    db,
    *,
    owner_id,
    strategy_execution_id,
    finding_type,
    description,
    idempotency_key,
    trace_event_id=None,
    justified=False,
    evidence_id=None,
    basis="unknown",
    provenance=None,
):
    trace = (
        _owned(db, WorkTraceEvent, owner_id, trace_event_id, "trace event")
        if trace_event_id
        else None
    )
    if trace and trace.strategy_execution_id != strategy_execution_id:
        raise WorkIntelligenceError("trace event does not belong to strategy execution")
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    return _record_for_execution(
        db,
        WorkStrategyFinding,
        owner_id=owner_id,
        strategy_execution_id=strategy_execution_id,
        idempotency_key=idempotency_key,
        values=dict(
            trace_event_id=trace.id if trace else None,
            finding_type=finding_type,
            description=description,
            justified=justified,
            evidence_id=evidence.id if evidence else None,
            basis=basis,
            provenance=provenance or {},
        ),
    )


def record_verification_obligation(
    db,
    *,
    owner_id,
    strategy_execution_id,
    requirement_kind,
    description,
    idempotency_key,
    required=True,
    source_task_id=None,
    basis="unknown",
    provenance=None,
):
    binding = _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    task = _ref(db, owner_id, "mainai_task", source_task_id)
    if task:
        execution = _owned(
            db, IntelligenceExecution, owner_id, binding.execution_id, "execution"
        )
        if task.id != execution.task_id:
            raise WorkIntelligenceError(
                "verification obligation task does not belong to the execution"
            )
    return _record_for_execution(
        db,
        WorkVerificationObligation,
        owner_id=owner_id,
        strategy_execution_id=strategy_execution_id,
        idempotency_key=idempotency_key,
        values=dict(
            requirement_kind=requirement_kind,
            description=description,
            required=required,
            source_task_id=task.id if task else None,
            basis=basis,
            provenance=provenance or {},
        ),
    )


def record_verification_observation(
    db,
    *,
    owner_id,
    obligation_id,
    status,
    reason,
    idempotency_key,
    evidence_id=None,
):
    obligation = _owned(
        db,
        WorkVerificationObligation,
        owner_id,
        obligation_id,
        "verification obligation",
    )
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    values = dict(
        status=status, reason=reason, evidence_id=evidence.id if evidence else None
    )
    existing = db.execute(
        select(WorkVerificationObservation).where(
            WorkVerificationObservation.owner_id == owner_id,
            WorkVerificationObservation.obligation_id == obligation.id,
            WorkVerificationObservation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    row = WorkVerificationObservation(
        owner_id=owner_id,
        obligation_id=obligation.id,
        idempotency_key=idempotency_key,
        **values,
    )
    db.add(row)
    db.flush()
    return row


def record_stopping_decision(
    db,
    *,
    owner_id,
    strategy_execution_id,
    decision_type,
    reason,
    idempotency_key,
    subsequent_outcome=None,
    evidence_id=None,
    basis="unknown",
):
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    return _record_for_execution(
        db,
        WorkStoppingDecision,
        owner_id=owner_id,
        strategy_execution_id=strategy_execution_id,
        idempotency_key=idempotency_key,
        values=dict(
            decision_type=decision_type,
            reason=reason,
            subsequent_outcome=subsequent_outcome,
            evidence_id=evidence.id if evidence else None,
            basis=basis,
        ),
    )


def record_specialist_contribution(
    db,
    *,
    owner_id,
    strategy_execution_id,
    specialist_execution_id,
    purpose,
    idempotency_key,
    contribution="unknown",
    evidence_available_before=None,
    evidence_id=None,
    duration_ms=None,
):
    binding = _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    specialist = _owned(
        db,
        IntelligenceExecution,
        owner_id,
        specialist_execution_id,
        "specialist execution",
    )
    if specialist.id == binding.execution_id:
        raise WorkIntelligenceError(
            "specialist contribution must use a distinct execution"
        )
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    return _record_for_execution(
        db,
        WorkSpecialistContribution,
        owner_id=owner_id,
        strategy_execution_id=binding.id,
        idempotency_key=idempotency_key,
        values=dict(
            specialist_execution_id=specialist.id,
            purpose=purpose,
            contribution=contribution,
            evidence_available_before=evidence_available_before or {},
            evidence_id=evidence.id if evidence else None,
            duration_ms=duration_ms,
        ),
    )


def link_strategy_lesson(
    db, *, owner_id, strategy_id, engineering_lesson_id, relation, evidence_id=None
):
    strategy = _owned(db, WorkStrategy, owner_id, strategy_id, "strategy")
    if db.get(EngineeringLesson, engineering_lesson_id) is None:
        raise WorkIntelligenceError("engineering lesson does not exist")
    evidence = _ref(db, owner_id, "intelligence_evidence", evidence_id)
    row = db.get(WorkStrategyLessonLink, (owner_id, strategy.id, engineering_lesson_id))
    values = dict(relation=relation, evidence_id=evidence.id if evidence else None)
    if row:
        return _same(row, values)
    row = WorkStrategyLessonLink(
        owner_id=owner_id,
        strategy_id=strategy.id,
        engineering_lesson_id=engineering_lesson_id,
        **values,
    )
    db.add(row)
    db.flush()
    return row


@dataclass(frozen=True)
class VerificationState:
    required: int
    passed: int
    failed: int
    missing: int
    unknown: int
    obligations_satisfied: bool


def verification_state(db, *, owner_id, strategy_execution_id):
    _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    obligations = list(
        db.execute(
            select(WorkVerificationObligation).where(
                WorkVerificationObligation.owner_id == owner_id,
                WorkVerificationObligation.strategy_execution_id
                == strategy_execution_id,
                WorkVerificationObligation.required.is_(True),
            )
        ).scalars()
    )
    counts = {"passed": 0, "failed": 0, "missing": 0, "unknown": 0}
    for obligation in obligations:
        observations = list(
            db.execute(
                select(WorkVerificationObservation)
                .where(
                    WorkVerificationObservation.owner_id == owner_id,
                    WorkVerificationObservation.obligation_id == obligation.id,
                )
                .order_by(
                    WorkVerificationObservation.observed_at,
                    WorkVerificationObservation.id,
                )
            ).scalars()
        )
        status = observations[-1].status if observations else "missing"
        if status == "performed_passed":
            counts["passed"] += 1
        elif status == "performed_failed":
            counts["failed"] += 1
        elif status == "missing":
            counts["missing"] += 1
        else:
            counts["unknown"] += 1
    return VerificationState(
        required=len(obligations),
        obligations_satisfied=bool(obligations)
        and counts["passed"] == len(obligations),
        **counts,
    )


def ordered_trace(db, *, owner_id, strategy_execution_id):
    _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    return list(
        db.execute(
            select(WorkTraceEvent)
            .where(
                WorkTraceEvent.owner_id == owner_id,
                WorkTraceEvent.strategy_execution_id == strategy_execution_id,
            )
            .order_by(WorkTraceEvent.sequence_number)
        ).scalars()
    )


def strategy_history(db, *, owner_id, strategy_key):
    return list(
        db.execute(
            select(WorkStrategy)
            .where(
                WorkStrategy.owner_id == owner_id,
                WorkStrategy.strategy_key == strategy_key,
            )
            .order_by(WorkStrategy.version)
        ).scalars()
    )


def raw_strategy_observations(db, *, owner_id, strategy_execution_id):
    binding = _owned(
        db, WorkStrategyExecution, owner_id, strategy_execution_id, "strategy execution"
    )
    return {
        "binding": binding,
        "trace": ordered_trace(db, owner_id=owner_id, strategy_execution_id=binding.id),
        "efficiency": list(
            db.execute(
                select(WorkEfficiencyObservation).where(
                    WorkEfficiencyObservation.owner_id == owner_id,
                    WorkEfficiencyObservation.strategy_execution_id == binding.id,
                )
            ).scalars()
        ),
        "quality": verification_state(
            db, owner_id=owner_id, strategy_execution_id=binding.id
        ),
        "findings": list(
            db.execute(
                select(WorkStrategyFinding).where(
                    WorkStrategyFinding.owner_id == owner_id,
                    WorkStrategyFinding.strategy_execution_id == binding.id,
                )
            ).scalars()
        ),
    }
