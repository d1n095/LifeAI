"""Provider-independent synthesis recipes over existing strategy/evidence truth."""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select

from app.models.intelligence_governance import (
    IntelligenceEvidence,
    IntelligenceExecution,
    IntelligenceIdea,
)
from app.models.mainai_execution import EngineeringLesson, MainAITask
from app.models.problem_learning import (
    LifeProblem,
    LifeProblemAssumption,
    LifeSolutionComponent,
)
from app.models.strategy_evaluation import (
    StrategyComparison,
    StrategyExperiment,
    StrategyLearningObservation,
    StrategyPromotionCandidate,
)
from app.models.strategy_synthesis import (
    StrategySynthesisCase,
    StrategySynthesisComponent,
    StrategySynthesisConflict,
    StrategySynthesisEvaluationLink,
    StrategySynthesisEvent,
    StrategySynthesisInput,
    StrategySynthesisLessonLink,
    StrategySynthesisMaterialization,
)
from app.models.user import User
from app.models.work_intelligence import (
    WorkSpecialistContribution,
    WorkStoppingDecision,
    WorkStrategy,
    WorkStrategyExecution,
)
from app.work_intelligence import create_strategy


class StrategySynthesisError(ValueError):
    pass


SOURCE_MODELS = {
    "work_strategy": (WorkStrategy, "work_strategy_id"),
    "strategy_execution": (WorkStrategyExecution, "strategy_execution_id"),
    "intelligence_idea": (IntelligenceIdea, "intelligence_idea_id"),
    "intelligence_evidence": (IntelligenceEvidence, "intelligence_evidence_id"),
    "intelligence_execution": (IntelligenceExecution, "intelligence_execution_id"),
    "strategy_comparison": (StrategyComparison, "comparison_id"),
    "strategy_experiment": (StrategyExperiment, "experiment_id"),
    "engineering_lesson": (EngineeringLesson, "engineering_lesson_id"),
    "solution_component": (LifeSolutionComponent, "solution_component_id"),
    "assumption": (LifeProblemAssumption, "assumption_id"),
    "specialist_contribution": (
        WorkSpecialistContribution,
        "specialist_contribution_id",
    ),
    "learning_observation": (
        StrategyLearningObservation,
        "learning_observation_id",
    ),
    "stopping_decision": (WorkStoppingDecision, "stopping_decision_id"),
}


def _same(row, values):
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise StrategySynthesisError(
            f"idempotency key reused with different fields: {', '.join(sorted(differing))}"
        )
    return row


def _owned(db, model, owner_id, row_id, label, *, lock=False):
    query = select(model).where(model.id == row_id)
    if hasattr(model, "owner_id"):
        query = query.where(model.owner_id == owner_id)
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise StrategySynthesisError(f"{label} is missing or belongs to another owner")
    return row


def _idempotent(db, model, owner_id, key, values):
    row = db.execute(
        select(model).where(model.owner_id == owner_id, model.idempotency_key == key)
    ).scalar_one_or_none()
    if row:
        return _same(row, values)
    row = model(owner_id=owner_id, idempotency_key=key, **values)
    db.add(row)
    db.flush()
    return row


def _event(
    db,
    *,
    owner_id,
    case_id,
    event_type,
    idempotency_key,
    conflict_id=None,
    from_state=None,
    to_state=None,
    detail=None,
):
    return _idempotent(
        db,
        StrategySynthesisEvent,
        owner_id,
        idempotency_key,
        dict(
            case_id=case_id,
            conflict_id=conflict_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            detail=detail or {},
        ),
    )


def create_synthesis_case(
    db,
    *,
    owner_id,
    case_key,
    revision,
    candidate_strategy_key,
    candidate_strategy_version,
    purpose,
    quality_invariants,
    idempotency_key,
    improvement_dimensions=None,
    applicability=None,
    expected_benefits=None,
    expected_tradeoffs=None,
    predecessor_case_id=None,
    predecessor_strategy_id=None,
    task_id=None,
    problem_id=None,
    domain=None,
    risk_level="unknown",
    provenance=None,
):
    owner = db.execute(
        select(User).where(User.id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if owner is None:
        raise StrategySynthesisError("owner does not exist")
    predecessor_case = (
        _owned(
            db,
            StrategySynthesisCase,
            owner_id,
            predecessor_case_id,
            "predecessor synthesis case",
        )
        if predecessor_case_id
        else None
    )
    if predecessor_case and (
        predecessor_case.case_key != case_key or predecessor_case.revision >= revision
    ):
        raise StrategySynthesisError(
            "predecessor case must be an earlier revision of the same case key"
        )
    predecessor_strategy = (
        _owned(
            db,
            WorkStrategy,
            owner_id,
            predecessor_strategy_id,
            "predecessor strategy",
        )
        if predecessor_strategy_id
        else None
    )
    if predecessor_strategy and (
        predecessor_strategy.strategy_key != candidate_strategy_key
        or predecessor_strategy.version >= candidate_strategy_version
    ):
        raise StrategySynthesisError(
            "predecessor strategy must be an earlier version of the candidate strategy"
        )
    task = _owned(db, MainAITask, owner_id, task_id, "task") if task_id else None
    problem = (
        _owned(db, LifeProblem, owner_id, problem_id, "problem") if problem_id else None
    )
    immutable_values = dict(
        case_key=case_key,
        revision=revision,
        predecessor_case_id=predecessor_case.id if predecessor_case else None,
        predecessor_strategy_id=(
            predecessor_strategy.id if predecessor_strategy else None
        ),
        candidate_strategy_key=candidate_strategy_key,
        candidate_strategy_version=candidate_strategy_version,
        task_id=task.id if task else None,
        problem_id=problem.id if problem else None,
        domain=domain,
        risk_level=risk_level,
        purpose=purpose,
        improvement_dimensions=improvement_dimensions or [],
        quality_invariants=quality_invariants or [],
        applicability=applicability or {},
        expected_benefits=expected_benefits or [],
        expected_tradeoffs=expected_tradeoffs or [],
        provenance=provenance or {},
    )
    existing = db.execute(
        select(StrategySynthesisCase).where(
            StrategySynthesisCase.owner_id == owner_id,
            StrategySynthesisCase.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        row = _same(existing, immutable_values)
    else:
        row = StrategySynthesisCase(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            status="draft",
            next_component_sequence=1,
            **immutable_values,
        )
        db.add(row)
        db.flush()
    _event(
        db,
        owner_id=owner_id,
        case_id=row.id,
        event_type="created",
        to_state="draft",
        idempotency_key=f"{idempotency_key}:created",
    )
    return row


def add_synthesis_input(
    db,
    *,
    owner_id,
    case_id,
    source_kind,
    source_id,
    disposition,
    reason,
    idempotency_key,
    basis="unknown",
    supporting_evidence_id=None,
):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    spec = SOURCE_MODELS.get(source_kind)
    if spec is None:
        raise StrategySynthesisError(
            f"unsupported synthesis source kind: {source_kind}"
        )
    model, column = spec
    source = _owned(db, model, owner_id, source_id, source_kind.replace("_", " "))
    evidence = (
        _owned(
            db,
            IntelligenceEvidence,
            owner_id,
            supporting_evidence_id,
            "supporting evidence",
        )
        if supporting_evidence_id
        else None
    )
    source_values = {name: None for _, name in SOURCE_MODELS.values()}
    source_values[column] = source.id
    values = dict(
        case_id=case.id,
        source_kind=source_kind,
        disposition=disposition,
        reason=reason,
        basis=basis,
        supporting_evidence_id=evidence.id if evidence else None,
        **source_values,
    )
    existing = db.execute(
        select(StrategySynthesisInput).where(
            StrategySynthesisInput.owner_id == owner_id,
            StrategySynthesisInput.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)
    if case.status != "draft":
        raise StrategySynthesisError("inputs may be added only while the case is draft")
    row = _idempotent(db, StrategySynthesisInput, owner_id, idempotency_key, values)
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="input_added",
        idempotency_key=f"{idempotency_key}:event",
        detail={
            "input_id": str(row.id),
            "source_kind": source_kind,
            "source_id": str(source.id),
            "disposition": disposition,
        },
    )
    return row


def add_recipe_component(
    db,
    *,
    owner_id,
    case_id,
    input_id,
    component_kind,
    description,
    disposition,
    reason,
    idempotency_key,
    modification_intent=None,
    applicability=None,
    method_payload=None,
    assumption_id=None,
    evidence_id=None,
    basis="unknown",
):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    synthesis_input = _owned(
        db, StrategySynthesisInput, owner_id, input_id, "synthesis input"
    )
    if synthesis_input.case_id != case.id:
        raise StrategySynthesisError("synthesis input belongs to another case")
    assumption = (
        _owned(db, LifeProblemAssumption, owner_id, assumption_id, "assumption")
        if assumption_id
        else None
    )
    evidence = (
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
        if evidence_id
        else None
    )
    values = dict(
        case_id=case.id,
        input_id=synthesis_input.id,
        sequence_number=case.next_component_sequence,
        component_kind=component_kind,
        description=description,
        modification_intent=modification_intent,
        disposition=disposition,
        applicability=applicability or {},
        method_payload=method_payload or {},
        assumption_id=assumption.id if assumption else None,
        evidence_id=evidence.id if evidence else None,
        reason=reason,
        basis=basis,
    )
    existing = db.execute(
        select(StrategySynthesisComponent).where(
            StrategySynthesisComponent.owner_id == owner_id,
            StrategySynthesisComponent.case_id == case.id,
            StrategySynthesisComponent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        comparison = dict(values)
        comparison.pop("sequence_number")
        return _same(existing, comparison)
    if case.status != "draft":
        raise StrategySynthesisError(
            "recipe components may be added only while the case is draft"
        )
    row = StrategySynthesisComponent(
        owner_id=owner_id, idempotency_key=idempotency_key, **values
    )
    case.next_component_sequence += 1
    db.add(row)
    db.flush()
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="component_added",
        idempotency_key=f"{idempotency_key}:event",
        detail={
            "component_id": str(row.id),
            "sequence_number": row.sequence_number,
            "disposition": disposition,
        },
    )
    return row


def ordered_recipe(db, *, owner_id, case_id):
    _owned(db, StrategySynthesisCase, owner_id, case_id, "synthesis case")
    return list(
        db.execute(
            select(StrategySynthesisComponent)
            .where(
                StrategySynthesisComponent.owner_id == owner_id,
                StrategySynthesisComponent.case_id == case_id,
            )
            .order_by(
                StrategySynthesisComponent.sequence_number,
                StrategySynthesisComponent.id,
            )
        ).scalars()
    )


def reorder_recipe(db, *, owner_id, case_id, ordered_component_ids, idempotency_key):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    if case.status != "draft":
        raise StrategySynthesisError("recipe may be reordered only while draft")
    current = ordered_recipe(db, owner_id=owner_id, case_id=case.id)
    current_ids = [row.id for row in current]
    if len(set(ordered_component_ids)) != len(ordered_component_ids) or set(
        ordered_component_ids
    ) != set(current_ids):
        raise StrategySynthesisError(
            "reorder must contain every recipe component exactly once"
        )
    existing_event = db.execute(
        select(StrategySynthesisEvent).where(
            StrategySynthesisEvent.owner_id == owner_id,
            StrategySynthesisEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    requested_after = [str(value) for value in ordered_component_ids]
    if existing_event:
        _same(
            existing_event,
            {
                "case_id": case.id,
                "conflict_id": None,
                "event_type": "recipe_reordered",
                "from_state": None,
                "to_state": None,
            },
        )
        if existing_event.detail.get("after") != requested_after:
            raise StrategySynthesisError(
                "idempotency key reused with a different recipe order"
            )
        return existing_event
    target_detail = {
        "before": [str(value) for value in current_ids],
        "after": requested_after,
    }
    by_id = {row.id: row for row in current}
    for sequence, component_id in enumerate(ordered_component_ids, 1):
        by_id[component_id].sequence_number = sequence
    db.flush()
    return _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="recipe_reordered",
        idempotency_key=idempotency_key,
        detail=target_detail,
    )


def record_conflict(
    db,
    *,
    owner_id,
    case_id,
    description,
    idempotency_key,
    severity="hard",
    left_component_id=None,
    right_component_id=None,
    assumption_id=None,
    evidence_id=None,
):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    if case.status != "draft":
        raise StrategySynthesisError("conflicts may be recorded only while draft")
    left = (
        _owned(
            db,
            StrategySynthesisComponent,
            owner_id,
            left_component_id,
            "left component",
        )
        if left_component_id
        else None
    )
    right = (
        _owned(
            db,
            StrategySynthesisComponent,
            owner_id,
            right_component_id,
            "right component",
        )
        if right_component_id
        else None
    )
    if any(row and row.case_id != case.id for row in (left, right)):
        raise StrategySynthesisError("conflict component belongs to another case")
    assumption = (
        _owned(db, LifeProblemAssumption, owner_id, assumption_id, "assumption")
        if assumption_id
        else None
    )
    evidence = (
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
        if evidence_id
        else None
    )
    values = dict(
        case_id=case.id,
        left_component_id=left.id if left else None,
        right_component_id=right.id if right else None,
        assumption_id=assumption.id if assumption else None,
        severity=severity,
        status="unresolved",
        description=description,
        resolution_reason=None,
        evidence_id=evidence.id if evidence else None,
    )
    row = _idempotent(db, StrategySynthesisConflict, owner_id, idempotency_key, values)
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        conflict_id=row.id,
        event_type="conflict_recorded",
        idempotency_key=f"{idempotency_key}:event",
        to_state="unresolved",
    )
    return row


def transition_conflict(
    db,
    *,
    owner_id,
    conflict_id,
    status,
    resolution_reason,
    idempotency_key,
):
    conflict = _owned(
        db,
        StrategySynthesisConflict,
        owner_id,
        conflict_id,
        "synthesis conflict",
        lock=True,
    )
    existing = db.execute(
        select(StrategySynthesisEvent).where(
            StrategySynthesisEvent.owner_id == owner_id,
            StrategySynthesisEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        _same(
            existing,
            dict(
                case_id=conflict.case_id,
                conflict_id=conflict.id,
                event_type="conflict_state_changed",
                from_state=existing.from_state,
                to_state=status,
                detail={"resolution_reason": resolution_reason},
            ),
        )
        return conflict, existing
    if conflict.status != "unresolved" or status not in {
        "resolved",
        "accepted_risk",
        "invalidated",
    }:
        raise StrategySynthesisError("invalid synthesis conflict transition")
    old = conflict.status
    conflict.status = status
    conflict.resolution_reason = resolution_reason
    event = _event(
        db,
        owner_id=owner_id,
        case_id=conflict.case_id,
        conflict_id=conflict.id,
        event_type="conflict_state_changed",
        idempotency_key=idempotency_key,
        from_state=old,
        to_state=status,
        detail={"resolution_reason": resolution_reason},
    )
    db.flush()
    return conflict, event


CASE_TRANSITIONS = {
    "draft": {"ready", "invalidated", "cancelled"},
    "ready": {"assembled", "invalidated", "cancelled"},
    "assembled": {"completed", "invalidated"},
    "invalidated": set(),
    "cancelled": set(),
    "completed": set(),
}


def readiness(db, *, owner_id, case_id):
    case = _owned(db, StrategySynthesisCase, owner_id, case_id, "synthesis case")
    components = ordered_recipe(db, owner_id=owner_id, case_id=case.id)
    conflicts = list(
        db.execute(
            select(StrategySynthesisConflict).where(
                StrategySynthesisConflict.owner_id == owner_id,
                StrategySynthesisConflict.case_id == case.id,
                StrategySynthesisConflict.status == "unresolved",
            )
        ).scalars()
    )
    blockers = []
    if not any(row.disposition in {"included", "modified"} for row in components):
        blockers.append("no_included_or_modified_component")
    if not case.quality_invariants:
        blockers.append("missing_quality_invariants")
    if any(row.severity == "hard" for row in conflicts):
        blockers.append("unresolved_hard_conflict")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "unresolved_conflict_ids": [str(row.id) for row in conflicts],
    }


def transition_case(db, *, owner_id, case_id, status, idempotency_key):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    existing = db.execute(
        select(StrategySynthesisEvent).where(
            StrategySynthesisEvent.owner_id == owner_id,
            StrategySynthesisEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        _same(
            existing,
            {
                "case_id": case.id,
                "conflict_id": None,
                "event_type": "state_changed",
                "to_state": status,
                "detail": {},
            },
        )
        return case, existing
    if status not in CASE_TRANSITIONS[case.status]:
        raise StrategySynthesisError(
            f"invalid synthesis transition {case.status} -> {status}"
        )
    if status == "ready":
        state = readiness(db, owner_id=owner_id, case_id=case.id)
        if not state["ready"]:
            raise StrategySynthesisError(
                f"synthesis is not ready: {', '.join(state['blockers'])}"
            )
    old, case.status = case.status, status
    event = _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="state_changed",
        idempotency_key=idempotency_key,
        from_state=old,
        to_state=status,
    )
    db.flush()
    return case, event


def _recipe_document(case, components):
    return {
        "case_id": str(case.id),
        "case_key": case.case_key,
        "revision": case.revision,
        "predecessor_strategy_id": (
            str(case.predecessor_strategy_id) if case.predecessor_strategy_id else None
        ),
        "quality_invariants": case.quality_invariants,
        "applicability": case.applicability,
        "components": [
            {
                "id": str(row.id),
                "input_id": str(row.input_id),
                "sequence": row.sequence_number,
                "kind": row.component_kind,
                "description": row.description,
                "modification_intent": row.modification_intent,
                "disposition": row.disposition,
                "applicability": row.applicability,
                "method_payload": row.method_payload,
                "assumption_id": str(row.assumption_id) if row.assumption_id else None,
                "evidence_id": str(row.evidence_id) if row.evidence_id else None,
            }
            for row in components
        ],
    }


def materialize_candidate(db, *, owner_id, case_id, idempotency_key):
    case = _owned(
        db, StrategySynthesisCase, owner_id, case_id, "synthesis case", lock=True
    )
    existing = db.execute(
        select(StrategySynthesisMaterialization).where(
            StrategySynthesisMaterialization.owner_id == owner_id,
            StrategySynthesisMaterialization.case_id == case.id,
        )
    ).scalar_one_or_none()
    if existing:
        if existing.idempotency_key != idempotency_key:
            raise StrategySynthesisError(
                "synthesis case is already materialized with another idempotency key"
            )
        return existing
    if case.status != "ready":
        raise StrategySynthesisError("only a ready synthesis case may be materialized")
    state = readiness(db, owner_id=owner_id, case_id=case.id)
    if not state["ready"]:
        raise StrategySynthesisError(
            f"synthesis is not ready: {', '.join(state['blockers'])}"
        )
    recipe = ordered_recipe(db, owner_id=owner_id, case_id=case.id)
    usable = [row for row in recipe if row.disposition in {"included", "modified"}]
    document = _recipe_document(case, recipe)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    strategy = create_strategy(
        db,
        owner_id=owner_id,
        strategy_key=case.candidate_strategy_key,
        version=case.candidate_strategy_version,
        predecessor_id=case.predecessor_strategy_id,
        work_category="synthesized_candidate",
        ordered_phases=[
            {
                "component_id": str(row.id),
                "kind": row.component_kind,
                "description": row.description,
                "modification_intent": row.modification_intent,
                "method": row.method_payload,
            }
            for row in usable
        ],
        tool_sequence=[
            row.method_payload["tool_identity"]
            for row in usable
            if row.method_payload.get("tool_identity")
        ],
        methods={
            "synthesis_case_id": str(case.id),
            "recipe_fingerprint": fingerprint,
            "quality_invariants": case.quality_invariants,
            "component_ids": [str(row.id) for row in usable],
        },
        environment_assumptions={"applicability": case.applicability},
        classification_basis="deterministic",
        provenance={
            "kind": "strategy_synthesis",
            "case_id": str(case.id),
            "recipe_fingerprint": fingerprint,
        },
        idempotency_key=f"synthesis:{case.id}:{fingerprint}",
    )
    row = StrategySynthesisMaterialization(
        owner_id=owner_id,
        case_id=case.id,
        strategy_id=strategy.id,
        recipe_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
    )
    db.add(row)
    case.status = "assembled"
    db.flush()
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="materialized",
        idempotency_key=f"{idempotency_key}:event",
        from_state="ready",
        to_state="assembled",
        detail={
            "strategy_id": str(strategy.id),
            "recipe_fingerprint": fingerprint,
        },
    )
    return row


def link_evaluation(
    db,
    *,
    owner_id,
    materialization_id,
    relation,
    target_id,
    idempotency_key,
):
    materialization = _owned(
        db,
        StrategySynthesisMaterialization,
        owner_id,
        materialization_id,
        "materialization",
        lock=True,
    )
    targets = {
        "experimented_by": (StrategyExperiment, "experiment_id"),
        "evaluated_by": (StrategyComparison, "comparison_id"),
        "promotion_considered_by": (
            StrategyPromotionCandidate,
            "promotion_candidate_id",
        ),
    }
    if relation not in targets:
        raise StrategySynthesisError("unsupported synthesis evaluation relation")
    model, column = targets[relation]
    target = _owned(db, model, owner_id, target_id, relation)
    strategy_id = materialization.strategy_id
    if relation == "experimented_by" and target.challenger_strategy_id != strategy_id:
        raise StrategySynthesisError(
            "experiment challenger is not the synthesized strategy"
        )
    if relation == "evaluated_by":
        challenger = _owned(
            db,
            WorkStrategyExecution,
            owner_id,
            target.challenger_binding_id,
            "comparison challenger binding",
        )
        if challenger.strategy_id != strategy_id:
            raise StrategySynthesisError(
                "comparison challenger is not the synthesized strategy"
            )
    if relation == "promotion_considered_by" and target.strategy_id != strategy_id:
        raise StrategySynthesisError(
            "promotion candidate is not the synthesized strategy"
        )
    values = {
        "materialization_id": materialization.id,
        "experiment_id": None,
        "comparison_id": None,
        "promotion_candidate_id": None,
        "relation": relation,
    }
    values[column] = target.id
    row = _idempotent(
        db, StrategySynthesisEvaluationLink, owner_id, idempotency_key, values
    )
    case = _owned(
        db,
        StrategySynthesisCase,
        owner_id,
        materialization.case_id,
        "synthesis case",
    )
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="evaluation_linked",
        idempotency_key=f"{idempotency_key}:event",
        detail={"relation": relation, "target_id": str(target.id)},
    )
    return row


def link_engineering_lesson(
    db,
    *,
    owner_id,
    case_id,
    engineering_lesson_id,
    relation,
    idempotency_key,
    component_id=None,
    evidence_id=None,
):
    case = _owned(db, StrategySynthesisCase, owner_id, case_id, "synthesis case")
    lesson = _owned(
        db, EngineeringLesson, owner_id, engineering_lesson_id, "engineering lesson"
    )
    component = (
        _owned(
            db,
            StrategySynthesisComponent,
            owner_id,
            component_id,
            "synthesis component",
        )
        if component_id
        else None
    )
    if component and component.case_id != case.id:
        raise StrategySynthesisError("lesson component belongs to another case")
    evidence = (
        _owned(db, IntelligenceEvidence, owner_id, evidence_id, "evidence")
        if evidence_id
        else None
    )
    row = _idempotent(
        db,
        StrategySynthesisLessonLink,
        owner_id,
        idempotency_key,
        dict(
            case_id=case.id,
            component_id=component.id if component else None,
            engineering_lesson_id=lesson.id,
            evidence_id=evidence.id if evidence else None,
            relation=relation,
        ),
    )
    _event(
        db,
        owner_id=owner_id,
        case_id=case.id,
        event_type="lesson_linked",
        idempotency_key=f"{idempotency_key}:event",
        detail={
            "lesson_id": str(lesson.id),
            "component_id": str(component.id) if component else None,
            "relation": relation,
        },
    )
    return row


@dataclass(frozen=True)
class SynthesisExplanation:
    case: StrategySynthesisCase
    predecessors: list
    inputs: list
    components: list
    conflicts: list
    materialization: StrategySynthesisMaterialization | None
    evaluations: list
    lessons: list
    readiness: dict
    truncated: bool


def explain_synthesis(db, *, owner_id, case_id, max_depth=8, max_items=200):
    if max_depth < 1 or max_items < 1:
        raise StrategySynthesisError("explainability bounds must be positive")
    case = _owned(db, StrategySynthesisCase, owner_id, case_id, "synthesis case")
    predecessors, visited, current = [], {case.id}, case
    truncated = False
    for _ in range(max_depth):
        if not current.predecessor_case_id:
            break
        if current.predecessor_case_id in visited:
            truncated = True
            break
        current = _owned(
            db,
            StrategySynthesisCase,
            owner_id,
            current.predecessor_case_id,
            "predecessor synthesis case",
        )
        visited.add(current.id)
        predecessors.append(current)
    else:
        truncated = bool(current.predecessor_case_id)
    inputs = list(
        db.execute(
            select(StrategySynthesisInput)
            .where(
                StrategySynthesisInput.owner_id == owner_id,
                StrategySynthesisInput.case_id == case.id,
            )
            .order_by(StrategySynthesisInput.created_at, StrategySynthesisInput.id)
            .limit(max_items)
        ).scalars()
    )
    components = ordered_recipe(db, owner_id=owner_id, case_id=case.id)[:max_items]
    conflicts = list(
        db.execute(
            select(StrategySynthesisConflict)
            .where(
                StrategySynthesisConflict.owner_id == owner_id,
                StrategySynthesisConflict.case_id == case.id,
            )
            .order_by(
                StrategySynthesisConflict.created_at, StrategySynthesisConflict.id
            )
            .limit(max_items)
        ).scalars()
    )
    materialization = db.execute(
        select(StrategySynthesisMaterialization).where(
            StrategySynthesisMaterialization.owner_id == owner_id,
            StrategySynthesisMaterialization.case_id == case.id,
        )
    ).scalar_one_or_none()
    evaluations = []
    if materialization:
        evaluations = list(
            db.execute(
                select(StrategySynthesisEvaluationLink)
                .where(
                    StrategySynthesisEvaluationLink.owner_id == owner_id,
                    StrategySynthesisEvaluationLink.materialization_id
                    == materialization.id,
                )
                .order_by(StrategySynthesisEvaluationLink.created_at)
                .limit(max_items)
            ).scalars()
        )
    lessons = list(
        db.execute(
            select(StrategySynthesisLessonLink)
            .where(
                StrategySynthesisLessonLink.owner_id == owner_id,
                StrategySynthesisLessonLink.case_id == case.id,
            )
            .order_by(StrategySynthesisLessonLink.created_at)
            .limit(max_items)
        ).scalars()
    )
    return SynthesisExplanation(
        case=case,
        predecessors=predecessors,
        inputs=inputs,
        components=components,
        conflicts=conflicts,
        materialization=materialization,
        evaluations=evaluations,
        lessons=lessons,
        readiness=readiness(db, owner_id=owner_id, case_id=case.id),
        truncated=truncated,
    )
