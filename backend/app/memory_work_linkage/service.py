"""Stage C — Memory → Work linkage.

Orchestrates founder memory add/correct into inspectable links against existing goals/tasks/
candidates. NEVER authorizes work. NEVER calls create_goal/create_plan/replan/authorize_work_candidate.
Subordinate tasks require explicit founder authority + insert_plan_tasks.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation.normalize import jaccard, normalize_concept_text, token_set
from app.founder_memory import get_founder_memory
from app.mainai_execution.plan_insertion import (
    AUTHORIZED_KINDS,
    InsertedTaskSpec,
    PlanInsertionError,
    insert_plan_tasks,
)
from app.memory_threads.service import add_member, create_thread, thread_members
from app.memory_work_linkage.types import (
    AffectedWorkRef,
    ImpactKind,
    LinkageAction,
    LinkageResult,
    TimingClass,
)
from app.models.founder_memory import FounderMemoryNote
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.project_entities import ProjectEntity
from app.work_candidates.service import list_work_candidates, record_work_candidate

_MATCH_THRESHOLD = 0.35


class MemoryWorkLinkageError(ValueError):
    pass


def ensure_linkage_thread(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_id: uuid.UUID,
) -> uuid.UUID:
    thread = create_thread(
        db,
        owner_id=owner_id,
        idempotency_key=f"memory-work-link:{note_id}",
        system_label=f"memory_work_linkage:{note_id}",
        classification_basis="deterministic",
    )
    add_member(
        db,
        owner_id=owner_id,
        thread_id=thread.id,
        member_kind="founder_memory_note",
        member_ref_id=note_id,
        membership_basis="founder_added",
        classification_basis="deterministic",
        provenance={"stage": "C", "role": "source_note"},
        idempotency_key=f"mem-link-note:{note_id}",
        actor_type="system",
    )
    return thread.id


def find_affected_work(
    db: Session,
    *,
    owner_id: uuid.UUID,
    text: str,
    note_id: uuid.UUID | None = None,
) -> list[AffectedWorkRef]:
    """Provider-free search: token Jaccard vs goals/tasks/entities/candidates + thread co-members."""
    tokens = token_set(text)
    hits: dict[tuple[str, uuid.UUID], AffectedWorkRef] = {}

    def consider(kind: str, oid: uuid.UUID, status: str | None, title: str, reason: str):
        score = jaccard(tokens, token_set(title))
        if score < _MATCH_THRESHOLD and normalize_concept_text(title) != normalize_concept_text(text):
            return
        key = (kind, oid)
        prev = hits.get(key)
        if prev is None or score > prev.score:
            hits[key] = AffectedWorkRef(kind=kind, id=oid, status=status, score=score, reason=reason)

    goals = db.execute(select(MainAIGoal).where(MainAIGoal.owner_id == owner_id)).scalars().all()
    for goal in goals:
        status = getattr(goal.status, "value", str(goal.status))
        consider("mainai_goal", goal.id, status, goal.title or "", "goal_title_match")

    tasks = db.execute(select(MainAITask).where(MainAITask.owner_id == owner_id)).scalars().all()
    for task in tasks:
        status = getattr(task.status, "value", str(task.status))
        consider("mainai_task", task.id, status, task.description or "", "task_description_match")

    entities = db.execute(
        select(ProjectEntity).where(
            ProjectEntity.owner_id == owner_id,
            ProjectEntity.status.in_(("active", "proposed")),
        )
    ).scalars().all()
    for entity in entities:
        consider("project_entity", entity.id, entity.status, entity.title or "", "entity_title_match")

    for candidate in list_work_candidates(db, owner_id=owner_id):
        consider("work_candidate", candidate.id, candidate.status, candidate.title or "", "candidate_title_match")

    if note_id is not None:
        # Co-members on any thread that already includes this note.
        from app.models.memory_thread import MemoryThreadMember

        thread_ids = db.execute(
            select(MemoryThreadMember.thread_id).where(
                MemoryThreadMember.owner_id == owner_id,
                MemoryThreadMember.member_kind == "founder_memory_note",
                MemoryThreadMember.member_ref_id == str(note_id),
            )
        ).scalars().all()
        for tid in thread_ids:
            for member in thread_members(db, owner_id=owner_id, thread_id=tid):
                if member.member_kind in {"mainai_goal", "mainai_task", "work_candidate", "project_entity"}:
                    try:
                        mid = uuid.UUID(str(member.member_ref_id))
                    except (TypeError, ValueError):
                        continue
                    key = (member.member_kind, mid)
                    if key not in hits:
                        hits[key] = AffectedWorkRef(
                            kind=member.member_kind,
                            id=mid,
                            status=None,
                            score=1.0,
                            reason="thread_co_member",
                        )

    return sorted(hits.values(), key=lambda h: h.score, reverse=True)


def classify_memory_impact(
    *,
    note: FounderMemoryNote,
    affected: list[AffectedWorkRef],
    timing: TimingClass,
    is_correction: bool,
) -> list[ImpactKind]:
    impacts: list[ImpactKind] = []
    if timing == TimingClass.LATER:
        impacts.append(ImpactKind.PARK_LATER)
        return impacts
    if is_correction or note.note_type == "correction":
        impacts.append(ImpactKind.CORRECTION)
    active_tasks = [a for a in affected if a.kind == "mainai_task" and a.status in {"pending", "blocked", "ready", "running", "waiting"}]
    completed_tasks = [a for a in affected if a.kind == "mainai_task" and a.status == "completed"]
    if active_tasks:
        impacts.append(ImpactKind.AFFECTS_ACTIVE_TASK)
    if completed_tasks and not active_tasks:
        impacts.append(ImpactKind.COMPLETED_FOLLOWUP)
    if not impacts:
        impacts.append(ImpactKind.LINK_ONLY)
    return impacts


def link_note_to_affected(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    note_id: uuid.UUID,
    affected: list[AffectedWorkRef],
    timing: TimingClass,
    impacts: list[ImpactKind],
) -> list[LinkageAction]:
    actions: list[LinkageAction] = []
    for ref in affected:
        add_member(
            db,
            owner_id=owner_id,
            thread_id=thread_id,
            member_kind=ref.kind,
            member_ref_id=ref.id,
            membership_basis="same_goal" if ref.kind in {"mainai_goal", "mainai_task"} else "deterministic_relationship",
            classification_basis="deterministic",
            provenance={
                "stage": "C",
                "timing": timing.value,
                "impacts": [i.value for i in impacts],
                "score": ref.score,
                "reason": ref.reason,
                "source_note_id": str(note_id),
            },
            idempotency_key=f"mem-link:{note_id}:{ref.kind}:{ref.id}",
            actor_type="system",
        )
        actions.append(LinkageAction.LINKED_ONLY)
    return actions


def apply_memory_work_linkage(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_id: uuid.UUID,
    timing: TimingClass = TimingClass.NOW,
    is_correction: bool = False,
    insert_subordinate: bool = False,
    authority_kind: str | None = None,
    authorized_instruction_sha256: str | None = None,
    goal: MainAIGoal | None = None,
    plan: MainAIPlan | None = None,
    park_candidate: bool = True,
    supersede_candidate_ids: list[uuid.UUID] | None = None,
    contradict_entity_id: uuid.UUID | None = None,
) -> LinkageResult:
    """Main Stage C orchestrator. Default path never inserts tasks or authorizes work."""
    note = get_founder_memory(db, owner_id=owner_id, note_id=note_id)
    if note is None:
        raise MemoryWorkLinkageError("founder memory note not found")

    thread_id = ensure_linkage_thread(db, owner_id=owner_id, note_id=note_id)
    affected = find_affected_work(db, owner_id=owner_id, text=note.content, note_id=note_id)
    impacts = classify_memory_impact(note=note, affected=affected, timing=timing, is_correction=is_correction)
    actions = link_note_to_affected(
        db,
        owner_id=owner_id,
        thread_id=thread_id,
        note_id=note_id,
        affected=affected,
        timing=timing,
        impacts=impacts,
    )

    created_candidates: list[uuid.UUID] = []
    created_tasks: list[uuid.UUID] = []

    if supersede_candidate_ids:
        from app.work_candidates.service import supersede_work_candidate

        for cid in supersede_candidate_ids:
            supersede_work_candidate(
                db,
                owner_id=owner_id,
                candidate_id=cid,
                reason=f"superseded_by_memory_note:{note_id}",
            )
            actions.append(LinkageAction.CANDIDATE_SUPERSEDED)

    should_park = park_candidate and not insert_subordinate and (
        timing == TimingClass.LATER
        or ImpactKind.COMPLETED_FOLLOWUP in impacts
        or ImpactKind.PARK_LATER in impacts
        or ImpactKind.AFFECTS_ACTIVE_TASK in impacts
        or ImpactKind.CORRECTION in impacts
        or ImpactKind.LINK_ONLY in impacts
        or ImpactKind.CONTRADICTS_PLAN in impacts
    )
    if should_park and note.note_type in {"correction", "decision", "goal", "observation", "preference"}:
        entity_id = _best_entity_id(affected)
        if entity_id is not None:
            existing_same = _find_similar_unreviewed_candidate(db, owner_id=owner_id, text=note.content)
            if existing_same is not None:
                actions.append(LinkageAction.NOOP_SAME)
                impacts.append(ImpactKind.SAME_COLLAPSE)
                add_member(
                    db,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    member_kind="work_candidate",
                    member_ref_id=existing_same.id,
                    membership_basis="deterministic_relationship",
                    classification_basis="deterministic",
                    provenance={
                        "stage": "C",
                        "timing": timing.value,
                        "same_collapse": True,
                        "memory_note_id": str(note_id),
                    },
                    idempotency_key=f"mem-link-wc-same:{note_id}:{existing_same.id}",
                    actor_type="system",
                )
            else:
                candidate = record_work_candidate(
                    db,
                    owner_id=owner_id,
                    source_entity_id=entity_id,
                    title=f"[memory] {note.content[:180]}",
                    rationale=note.content,
                    idempotency_key=f"memory-work:{note_id}",
                    priority="low" if timing == TimingClass.LATER else "medium",
                    classifier_strategy="memory_work_linkage_v1",
                    classifier_confidence=0.5,
                    provenance={
                        "stage": "C",
                        "timing": timing.value,
                        "memory_note_id": str(note_id),
                        "not_now": timing == TimingClass.LATER,
                        "impacts": [i.value for i in impacts],
                    },
                )
                created_candidates.append(candidate.id)
                actions.append(LinkageAction.CANDIDATE_RECORDED)
                add_member(
                    db,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    member_kind="work_candidate",
                    member_ref_id=candidate.id,
                    membership_basis="deterministic_relationship",
                    classification_basis="deterministic",
                    provenance={"stage": "C", "timing": timing.value},
                    idempotency_key=f"mem-link-wc:{note_id}:{candidate.id}",
                    actor_type="system",
                )

    if contradict_entity_id is not None:
        from app.concept_reconciliation import relate_concepts

        target = contradict_entity_id
        from_entity = _best_entity_id(affected)
        if from_entity is not None and from_entity != target:
            relate_concepts(
                db,
                owner_id=owner_id,
                from_entity_id=from_entity,
                to_entity_id=target,
                relationship_type="contradicts",
                note=f"memory_note:{note_id}",
            )
        actions.append(LinkageAction.CONTRADICTION_FLAGGED)
        add_member(
            db,
            owner_id=owner_id,
            thread_id=thread_id,
            member_kind="project_entity",
            member_ref_id=target,
            membership_basis="deterministic_relationship",
            classification_basis="deterministic",
            provenance={"stage": "C", "impact": ImpactKind.CONTRADICTS_PLAN.value},
            idempotency_key=f"mem-link-contradict:{note_id}:{target}",
            actor_type="system",
        )
        impacts.append(ImpactKind.CONTRADICTS_PLAN)

    if insert_subordinate:
        if timing != TimingClass.NOW:
            raise MemoryWorkLinkageError("subordinate insert requires timing=now")
        if authority_kind not in AUTHORIZED_KINDS:
            raise MemoryWorkLinkageError("subordinate insert requires an AUTHORIZED_KINDS authority_kind")
        if goal is None or plan is None or not authorized_instruction_sha256:
            raise MemoryWorkLinkageError("subordinate insert requires goal, plan, and authorized_instruction_sha256")
        active = [a for a in affected if a.kind == "mainai_task" and a.status in {"pending", "blocked", "ready", "running"}]
        depends: tuple = ()
        if active:
            # Prefer depending on a pending/blocked task if present (safe edge target).
            for a in active:
                if a.status in {"pending", "blocked"}:
                    depends = (a.id,)
                    break
        try:
            inserted = insert_plan_tasks(
                db,
                goal=goal,
                plan=plan,
                authority_kind=authority_kind,
                authorized_instruction_sha256=authorized_instruction_sha256,
                idempotency_key=f"memory-subordinate:{note_id}",
                tasks=[
                    InsertedTaskSpec(
                        description=f"Address founder memory: {note.content[:200]}",
                        task_type="read_only_audit",
                        depends_on=depends,
                    )
                ],
                source_type="founder_memory",
                source_ref=str(note_id),
                reason="stage_C_memory_work_linkage",
                requested_by="founder",
            )
        except PlanInsertionError as exc:
            raise MemoryWorkLinkageError(str(exc)) from exc
        for task in inserted:
            created_tasks.append(task.id)
            actions.append(LinkageAction.SUBORDINATE_TASK_INSERTED)
            add_member(
                db,
                owner_id=owner_id,
                thread_id=thread_id,
                member_kind="mainai_task",
                member_ref_id=task.id,
                membership_basis="same_goal",
                classification_basis="deterministic",
                provenance={"stage": "C", "inserted_from_note": str(note_id)},
                idempotency_key=f"mem-link-new-task:{note_id}:{task.id}",
                actor_type="system",
            )

    return LinkageResult(
        note_id=note_id,
        thread_id=thread_id,
        timing=timing,
        impacts=impacts,
        actions=actions,
        created_task_ids=created_tasks,
        created_candidate_ids=created_candidates,
        affected=affected,
    )


def _best_entity_id(affected: list[AffectedWorkRef]) -> uuid.UUID | None:
    for ref in affected:
        if ref.kind == "project_entity":
            return ref.id
    return None


def _find_similar_unreviewed_candidate(db: Session, *, owner_id: uuid.UUID, text: str):
    """Collapse differently-worded repeats onto an existing unreviewed memory-parked candidate."""
    tokens = token_set(text)
    norm = normalize_concept_text(text)
    best = None
    best_score = 0.0
    for candidate in list_work_candidates(db, owner_id=owner_id, status="unreviewed"):
        # Only collapse against Stage C memory-parked candidates — never against
        # promote/reconcile producers (those remain separately inspectable).
        if candidate.classifier_strategy != "memory_work_linkage_v1":
            continue
        title = candidate.title or ""
        bare = title[9:] if title.startswith("[memory] ") else title
        score = jaccard(tokens, token_set(bare))
        if normalize_concept_text(bare) == norm:
            score = 1.0
        if score >= 0.75 and score > best_score:
            best = candidate
            best_score = score
    return best


def assert_no_forbidden_imports() -> None:
    """Static guard: Stage C source must not import authorize/replan/envelope paths."""
    root = Path(__file__).resolve().parent
    forbidden = (
        "authorize_work_candidate",
        "create_plan",
        "app.mainai_execution.replan",
        "app.execution_envelopes",
        "app.development_driver",
    )
    for path in root.glob("*.py"):
        text = path.read_text()
        for token in forbidden:
            if token in text and "assert_no_forbidden" not in text.split(token)[0][-80:]:
                # allow mentioning in this docstring/function only
                if path.name == "service.py" and token in (
                    "authorize_work_candidate",
                    "create_plan",
                ):
                    # mentioned in module docstring / comments as forbidden — verify not an import
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped.startswith("from ") or stripped.startswith("import "):
                            if token in stripped:
                                raise AssertionError(f"forbidden import of {token} in {path}")
                    continue
                if f"import {token}" in text or f"from " in text and token in text:
                    for line in text.splitlines():
                        if line.strip().startswith(("import ", "from ")) and token in line:
                            raise AssertionError(f"forbidden import of {token} in {path}")
