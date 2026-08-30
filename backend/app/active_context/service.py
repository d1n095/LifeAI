"""Bounded, cycle-safe, AI-independent Active Context resolver.

Only explicit database relationships are traversed. Memberships store references and paths,
never copied source content.
"""

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.active_context import ActiveContextEvent, ActiveContextMember, ActiveContextSet
from app.models.capability_reality import CapabilityRecord
from app.models.conversation import Conversation, Message
from app.models.diagnosis import DiagnosisRecord
from app.models.document import Document
from app.models.candidate_learning_signal import CandidateLearningSignal
from app.models.founder_memory import FounderMemoryNote
from app.models.intelligence_governance import (
    IntelligenceEvidence,
    IntelligenceExecution,
    IntelligenceIdea,
    IntelligenceIdeaLesson,
    IntelligenceInterpretation,
)
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.life_intent import LifeIntent, LifeIntentBlocker
from app.models.project_entities import ProjectEntity
from app.models.work_candidate import WorkCandidate
from app.models.mainai_execution import EngineeringLesson, MainAICheckpoint, MainAIGoal, MainAIPlan, MainAITask
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryRecord
from app.models.memory_source_unit import DocumentSourceUnit, MemorySourceUnit, MessageSourceUnit
from app.models.memory_thread import MemoryThread
from app.models.project import Project
from app.models.project_memory import ProjectNote
from app.models.problem_learning import (
    LifeApproachOutcome, LifeProblem, LifeProblemApproach, LifeProblemAssumption,
    LifeProblemDecision, LifeSolutionComponent,
)


SUPPORTED_TYPES = frozenset({
    "conversation", "message", "document", "knowledge_version", "knowledge_claim",
    "memory_source_unit", "document_source_unit", "message_source_unit", "mainai_goal",
    "mainai_plan", "mainai_task", "mainai_job", "mainai_checkpoint", "mainai_recovery",
    "engineering_lesson", "intelligence_execution", "intelligence_evidence",
    "intelligence_interpretation", "intelligence_idea", "project", "project_note", "memory_thread",
    "life_intent", "life_intent_blocker", "life_problem", "life_problem_approach",
    "life_solution_component", "life_problem_assumption", "life_problem_decision",
    "life_approach_outcome", "founder_memory_note", "diagnosis_record", "capability_record",
    "candidate_learning_signal", "work_candidate", "project_entity",
})
ANCHOR_TYPES = SUPPORTED_TYPES | {"explicit_topic"}


class InvalidContextReference(ValueError):
    pass


@dataclass(frozen=True)
class _Ref:
    object_type: str
    object_id: uuid.UUID


@dataclass(frozen=True)
class _Edge:
    target: _Ref
    relation: str


def _uuid_ref(object_type: str, object_ref: str | uuid.UUID) -> _Ref:
    if object_type not in SUPPORTED_TYPES:
        raise InvalidContextReference(f"unsupported context object type: {object_type}")
    try:
        return _Ref(object_type, uuid.UUID(str(object_ref)))
    except (TypeError, ValueError) as exc:
        raise InvalidContextReference(f"{object_type} requires a UUID reference") from exc


def _owned_row(db: Session, owner_id: uuid.UUID, ref: _Ref):
    oid = ref.object_id
    if ref.object_type == "conversation":
        return db.execute(select(Conversation).where(Conversation.id == oid, Conversation.user_id == owner_id)).scalar_one_or_none()
    if ref.object_type == "message":
        return db.execute(select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(
            Message.id == oid, Conversation.user_id == owner_id)).scalar_one_or_none()
    mappings = {
        "document": (Document, Document.uploaded_by), "knowledge_version": (KnowledgeVersion, KnowledgeVersion.owner_id),
        "knowledge_claim": (KnowledgeClaim, KnowledgeClaim.owner_id), "memory_source_unit": (MemorySourceUnit, MemorySourceUnit.owner_id),
        "document_source_unit": (DocumentSourceUnit, DocumentSourceUnit.owner_id),
        "message_source_unit": (MessageSourceUnit, MessageSourceUnit.owner_id), "mainai_goal": (MainAIGoal, MainAIGoal.owner_id),
        "mainai_plan": (MainAIPlan, MainAIPlan.owner_id), "mainai_task": (MainAITask, MainAITask.owner_id),
        "mainai_job": (MainAIJob, MainAIJob.owner_id), "mainai_checkpoint": (MainAICheckpoint, MainAICheckpoint.owner_id),
        "mainai_recovery": (MainAIRecoveryRecord, MainAIRecoveryRecord.owner_id),
        "intelligence_execution": (IntelligenceExecution, IntelligenceExecution.owner_id),
        "intelligence_evidence": (IntelligenceEvidence, IntelligenceEvidence.owner_id),
        "intelligence_interpretation": (IntelligenceInterpretation, IntelligenceInterpretation.owner_id),
        "intelligence_idea": (IntelligenceIdea, IntelligenceIdea.owner_id),
        "memory_thread": (MemoryThread, MemoryThread.owner_id),
        "life_intent": (LifeIntent, LifeIntent.owner_id),
        "life_intent_blocker": (LifeIntentBlocker, LifeIntentBlocker.owner_id),
        "life_problem": (LifeProblem, LifeProblem.owner_id),
        "life_problem_approach": (LifeProblemApproach, LifeProblemApproach.owner_id),
        "life_solution_component": (LifeSolutionComponent, LifeSolutionComponent.owner_id),
        "life_problem_assumption": (LifeProblemAssumption, LifeProblemAssumption.owner_id),
        "life_problem_decision": (LifeProblemDecision, LifeProblemDecision.owner_id),
        "life_approach_outcome": (LifeApproachOutcome, LifeApproachOutcome.owner_id),
        "founder_memory_note": (FounderMemoryNote, FounderMemoryNote.owner_id),
        "diagnosis_record": (DiagnosisRecord, DiagnosisRecord.owner_id),
        "capability_record": (CapabilityRecord, CapabilityRecord.owner_id),
        "candidate_learning_signal": (CandidateLearningSignal, CandidateLearningSignal.owner_id),
        "work_candidate": (WorkCandidate, WorkCandidate.owner_id),
        "project_entity": (ProjectEntity, ProjectEntity.owner_id),
    }
    if ref.object_type in mappings:
        model, owner_column = mappings[ref.object_type]
        pk = model.memory_source_id if ref.object_type in {"document_source_unit", "message_source_unit"} else model.id
        return db.execute(select(model).where(pk == oid, owner_column == owner_id)).scalar_one_or_none()
    if ref.object_type == "project":
        return db.execute(select(Project).where(Project.id == oid, Project.created_by == owner_id)).scalar_one_or_none()
    if ref.object_type == "engineering_lesson":
        return db.get(EngineeringLesson, oid)  # founder-wide canonical engineering knowledge
    if ref.object_type == "project_note":
        return db.get(ProjectNote, oid)  # founder-wide canonical project knowledge
    raise InvalidContextReference(f"unsupported context object type: {ref.object_type}")


def _require_ref(db: Session, owner_id: uuid.UUID, object_type: str, object_ref: str | uuid.UUID) -> tuple[_Ref, object]:
    ref = _uuid_ref(object_type, object_ref)
    row = _owned_row(db, owner_id, ref)
    if row is None:
        raise InvalidContextReference(f"{object_type} reference is missing or belongs to another owner")
    return ref, row


def _edges(db: Session, owner_id: uuid.UUID, ref: _Ref, row: object) -> list[_Edge]:
    edge: list[_Edge] = []

    def add(typ, oid, relation):
        if oid:
            edge.append(_Edge(_Ref(typ, oid), relation))

    if ref.object_type == "message":
        add("conversation", row.conversation_id, "same_conversation")
    elif ref.object_type == "mainai_task":
        add("mainai_plan", row.plan_id, "same_task")
        add("mainai_goal", row.goal_id, "same_goal")
        add("mainai_job", row.mainai_job_id, "same_task")
    elif ref.object_type == "mainai_plan":
        add("mainai_goal", row.goal_id, "same_goal")
    elif ref.object_type == "mainai_job":
        task_ids = db.execute(select(MainAITask.id).where(MainAITask.owner_id == owner_id, MainAITask.mainai_job_id == ref.object_id)).scalars()
        for task_id in task_ids:
            add("mainai_task", task_id, "same_task")
    elif ref.object_type == "mainai_checkpoint":
        add("mainai_goal", row.goal_id, "same_goal")
        add("mainai_task", row.task_id, "same_task")
    elif ref.object_type == "mainai_recovery":
        add("mainai_task", row.task_id, "same_task")
        add("mainai_job", row.job_id, "recovery_evidence")
    elif ref.object_type == "message_source_unit":
        add("memory_source_unit", row.memory_source_id, "source_of")
        add("message", row.message_id, "source_of")
        add("conversation", row.conversation_id, "same_conversation")
    elif ref.object_type == "document_source_unit":
        add("memory_source_unit", row.memory_source_id, "source_of")
        add("document", row.document_id, "source_of")
        add("knowledge_version", row.version_id, "source_of")
    elif ref.object_type == "knowledge_version":
        add("document", row.source_id, "source_of")
    elif ref.object_type == "knowledge_claim":
        add("document", row.source_id, "source_of")
        add("knowledge_version", row.version_id, "source_of")
        add("memory_source_unit", row.memory_source_id, "source_of")
    elif ref.object_type == "intelligence_execution":
        add("mainai_task", row.task_id, "same_task")
        add("mainai_job", row.job_id, "same_task")
    elif ref.object_type == "intelligence_evidence":
        add("intelligence_execution", row.execution_id, "evidence_for")
        add("intelligence_execution", row.observer_execution_id, "reviewed_by")
    elif ref.object_type == "intelligence_interpretation":
        add("intelligence_evidence", row.evidence_id, "derived_from")
        add("intelligence_interpretation", row.supersedes_id, "supersedes")
    elif ref.object_type == "intelligence_idea":
        add("intelligence_execution", row.execution_id, "originated_in")
        add("intelligence_evidence", row.evidence_id, "evidence_for")
        lessons = db.execute(select(IntelligenceIdeaLesson.engineering_lesson_id).where(
            IntelligenceIdeaLesson.owner_id == owner_id, IntelligenceIdeaLesson.idea_id == ref.object_id)).scalars()
        for lesson_id in lessons:
            add("engineering_lesson", lesson_id, "engineering_lesson")
    elif ref.object_type == "founder_memory_note":
        add("founder_memory_note", row.supersedes_note_id, "supersedes")
    elif ref.object_type == "diagnosis_record":
        add("diagnosis_record", row.supersedes_diagnosis_id, "supersedes")
        add("intelligence_evidence", row.proven_evidence_id, "proven_by")
    elif ref.object_type == "capability_record":
        add("intelligence_evidence", row.last_verification_evidence_id, "verified_by")
    return edge


def _event(db: Session, context: ActiveContextSet, action: str, *, member_id=None,
           actor_type="deterministic_resolver", detail=None) -> None:
    db.add(ActiveContextEvent(context_set_id=context.id, owner_id=context.owner_id,
                              member_id=member_id, action=action, actor_type=actor_type, detail=detail or {}))


def create_context_set(db: Session, *, owner_id: uuid.UUID, anchor_type: str, anchor_ref: str,
                       idempotency_key: str, label: str | None = None,
                       subject_basis: str = "unknown") -> ActiveContextSet:
    existing = db.execute(select(ActiveContextSet).where(
        ActiveContextSet.owner_id == owner_id, ActiveContextSet.idempotency_key == idempotency_key)).scalar_one_or_none()
    if existing:
        if (existing.anchor_type, existing.anchor_ref, existing.label, existing.subject_basis) != (
            anchor_type, str(anchor_ref), label, subject_basis
        ):
            raise ValueError("idempotency key reused for a different context set")
        return existing
    if anchor_type not in ANCHOR_TYPES:
        raise InvalidContextReference(f"unsupported context anchor type: {anchor_type}")
    if anchor_type == "explicit_topic":
        if not str(anchor_ref).strip() or subject_basis not in {"manual", "unknown"}:
            raise InvalidContextReference("explicit topics require a non-empty manual/unknown label")
    else:
        _require_ref(db, owner_id, anchor_type, anchor_ref)
    context = ActiveContextSet(owner_id=owner_id, label=label, anchor_type=anchor_type,
                               anchor_ref=str(anchor_ref), subject_basis=subject_basis,
                               idempotency_key=idempotency_key)
    db.add(context)
    db.flush()
    _event(db, context, "created", actor_type="founder" if subject_basis == "manual" else "system",
           detail={"anchor_type": anchor_type, "anchor_ref": str(anchor_ref)})
    return context


def refresh_context(db: Session, *, owner_id: uuid.UUID, context_set_id: uuid.UUID,
                    max_depth: int = 4, max_members: int = 100,
                    max_per_type: int = 30) -> list[ActiveContextMember]:
    if not (0 <= max_depth <= 10 and 1 <= max_members <= 500 and 1 <= max_per_type <= 200):
        raise ValueError("context bounds outside supported limits")
    context = db.execute(select(ActiveContextSet).where(
        ActiveContextSet.id == context_set_id, ActiveContextSet.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if context is None:
        raise InvalidContextReference("context set is missing or belongs to another owner")
    if context.anchor_type == "explicit_topic":
        roots: list[tuple[_Ref, object]] = []
    else:
        roots = [_require_ref(db, owner_id, context.anchor_type, context.anchor_ref)]

    now = datetime.utcnow()
    existing = {(m.object_type, m.object_ref): m for m in db.execute(select(ActiveContextMember).where(
        ActiveContextMember.context_set_id == context.id, ActiveContextMember.owner_id == owner_id)).scalars()}
    discovered: dict[tuple[str, str], tuple[int, str, list]] = {}
    counts: defaultdict[str, int] = defaultdict(int)
    queue = deque((ref, row, 0, [{"object_type": ref.object_type, "object_ref": str(ref.object_id), "relation": "anchor"}])
                  for ref, row in roots)
    visited: set[tuple[str, str]] = set()
    rank = 0
    while queue and len(discovered) < max_members:
        ref, row, depth, path = queue.popleft()
        key = (ref.object_type, str(ref.object_id))
        if key in visited:
            continue
        visited.add(key)
        if counts[ref.object_type] >= max_per_type:
            continue
        counts[ref.object_type] += 1
        reason = path[-1]["relation"]
        discovered[key] = (rank, reason, path)
        rank += 1
        if depth >= max_depth:
            continue
        for relation in _edges(db, owner_id, ref, row):
            target_key = (relation.target.object_type, str(relation.target.object_id))
            if target_key in visited:
                continue
            target_row = _owned_row(db, owner_id, relation.target)
            if target_row is not None:
                queue.append((relation.target, target_row, depth + 1,
                              [*path, {"object_type": relation.target.object_type,
                                      "object_ref": str(relation.target.object_id), "relation": relation.relation}]))

    for key, member in existing.items():
        if member.state == "active" and key not in discovered:
            member.state = "stale"
    for key, (member_rank, reason, path) in discovered.items():
        member = existing.get(key)
        if member is None:
            member = ActiveContextMember(
                context_set_id=context.id, owner_id=owner_id, object_type=key[0], object_ref=key[1],
                inclusion_reason=reason, relevance_basis="deterministic", authority="deterministic_source",
                rank=member_rank, state="active", activation_path=path,
                source_provenance={"object_type": key[0], "object_ref": key[1]},
                added_at=now, last_activated_at=now,
            )
            db.add(member)
            db.flush()
            existing[key] = member
            _event(db, context, "member_added", member_id=member.id, detail={"reason": reason, "path": path})
        elif member.state != "suppressed":
            if member.state == "stale":
                member.state = "active"
            member.rank, member.inclusion_reason = member_rank, reason
            member.activation_path, member.last_activated_at = path, now
    context.refreshed_at = now
    _event(db, context, "refreshed", detail={"max_depth": max_depth, "max_members": max_members,
                                              "max_per_type": max_per_type, "discovered": len(discovered)})
    db.flush()
    return current_members(db, owner_id=owner_id, context_set_id=context.id)


def _manual_state(db: Session, *, owner_id: uuid.UUID, context_set_id: uuid.UUID,
                  object_type: str, object_ref: str | uuid.UUID, state: str, action: str,
                  actor_type: str = "founder", event_detail: dict | None = None) -> ActiveContextMember:
    ref, _ = _require_ref(db, owner_id, object_type, object_ref)
    context = db.execute(select(ActiveContextSet).where(
        ActiveContextSet.id == context_set_id, ActiveContextSet.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if context is None:
        raise InvalidContextReference("context set is missing or belongs to another owner")
    member = db.execute(select(ActiveContextMember).where(
        ActiveContextMember.context_set_id == context.id, ActiveContextMember.object_type == object_type,
        ActiveContextMember.object_ref == str(ref.object_id)).with_for_update()).scalar_one_or_none()
    now = datetime.utcnow()
    if member is None:
        member = ActiveContextMember(
            context_set_id=context.id, owner_id=owner_id, object_type=object_type,
            object_ref=str(ref.object_id), inclusion_reason="manual_pin" if state == "pinned" else "manual_suppression",
            relevance_basis="manual", authority="founder", rank=0, state=state,
            activation_path=[{"object_type": object_type, "object_ref": str(ref.object_id), "relation": action}],
            source_provenance={"object_type": object_type, "object_ref": str(ref.object_id)},
            added_at=now, last_activated_at=now,
        )
        db.add(member)
        db.flush()
    else:
        member.state = state
        member.last_activated_at = now
        if state == "pinned":
            member.relevance_basis, member.authority, member.inclusion_reason = "manual", "founder", "manual_pin"
    detail = {"object_type": object_type, "object_ref": str(ref.object_id)}
    detail.update(event_detail or {})
    _event(db, context, action, member_id=member.id, actor_type=actor_type, detail=detail)
    db.flush()
    return member


def pin_object(db: Session, **kwargs) -> ActiveContextMember:
    return _manual_state(db, state="pinned", action="pinned", **kwargs)


def suppress_object(db: Session, **kwargs) -> ActiveContextMember:
    return _manual_state(db, state="suppressed", action="suppressed", **kwargs)


def unpin_object(db: Session, **kwargs) -> ActiveContextMember:
    return _manual_state(db, state="active", action="unpinned", **kwargs)


def unsuppress_object(db: Session, **kwargs) -> ActiveContextMember:
    return _manual_state(db, state="stale", action="unsuppressed", **kwargs)


def mark_noncurrent(db: Session, *, owner_id: uuid.UUID, context_set_id: uuid.UUID,
                    object_type: str, object_ref: str | uuid.UUID, state: str,
                    reason: str, actor_type: str = "founder") -> ActiveContextMember:
    if state not in {"stale", "superseded"} or not reason.strip():
        raise ValueError("non-current context requires stale/superseded state and a reason")
    member = _manual_state(db, owner_id=owner_id, context_set_id=context_set_id,
                           object_type=object_type, object_ref=object_ref,
                           state=state, action="state_changed", actor_type=actor_type,
                           event_detail={"state": state, "reason": reason})
    db.flush()
    return member


def current_members(db: Session, *, owner_id: uuid.UUID, context_set_id: uuid.UUID,
                    include_noncurrent: bool = False) -> list[ActiveContextMember]:
    states = ["active", "pinned"] if not include_noncurrent else ["active", "pinned", "suppressed", "stale", "superseded"]
    return list(db.execute(select(ActiveContextMember).where(
        ActiveContextMember.context_set_id == context_set_id,
        ActiveContextMember.owner_id == owner_id, ActiveContextMember.state.in_(states),
    ).order_by(ActiveContextMember.state.desc(), ActiveContextMember.rank, ActiveContextMember.added_at)).scalars())
