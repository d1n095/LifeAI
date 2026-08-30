"""Collect durable evidence rows for a recap window (read-only, no LLM)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import list_founder_memory
from app.models.mainai_execution import EngineeringLesson, MainAIGoal, MainAIPlan, MainAITask, MainAITaskEvent
from app.models.mainai_recovery import MainAIRecoveryEvent, MainAIRecoveryRecord
from app.models.memory_thread import MemoryThreadEvent
from app.models.project_entities import ProjectEntity
from app.project_memory import list_checkpoints, list_current_branch_pr_status, list_notes, list_sources
from app.temporal_intelligence.types import RecapEvidenceItem, TimeRange
from app.temporal_intelligence.windows import in_range
from app.work_candidates import list_work_candidates


def _ts(*candidates: datetime | None) -> datetime | None:
    for value in candidates:
        if value is not None:
            return value
    return None


def collect_founder_memory(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    for note in list_founder_memory(db, owner_id=owner_id):
        when = _ts(note.observed_at, note.created_at, note.valid_from)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="founder_memory_note",
                id=note.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=(note.content or "")[:200],
                status=note.status,
                owner_id=owner_id,
                source_table="founder_memory_notes",
                refs={"note_type": note.note_type, "supersedes_note_id": str(note.supersedes_note_id) if note.supersedes_note_id else None},
            )
        )
    return items


def collect_goals_plans_tasks(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    goals = db.execute(select(MainAIGoal).where(MainAIGoal.owner_id == owner_id)).scalars().all()
    for goal in goals:
        when = _ts(goal.completed_at, goal.started_at, goal.created_at)
        if in_range(when, rng):
            items.append(
                RecapEvidenceItem(
                    kind="mainai_goal",
                    id=goal.id,
                    occurred_at=when,  # type: ignore[arg-type]
                    title=goal.title or "",
                    status=getattr(goal.status, "value", str(goal.status)),
                    owner_id=owner_id,
                    source_table="mainai_goals",
                )
            )
        plans = db.execute(select(MainAIPlan).where(MainAIPlan.goal_id == goal.id)).scalars().all()
        for plan in plans:
            if in_range(plan.created_at, rng):
                items.append(
                    RecapEvidenceItem(
                        kind="mainai_plan",
                        id=plan.id,
                        occurred_at=plan.created_at,
                        title=f"plan v{getattr(plan, 'version', '?')} for {goal.title}",
                        status=getattr(plan.status, "value", str(plan.status)),
                        owner_id=owner_id,
                        source_table="mainai_plans",
                        refs={"goal_id": str(goal.id)},
                    )
                )
        tasks = db.execute(select(MainAITask).where(MainAITask.owner_id == owner_id, MainAITask.goal_id == goal.id)).scalars().all()
        for task in tasks:
            when = _ts(task.completed_at, task.started_at, task.created_at)
            if in_range(when, rng):
                items.append(
                    RecapEvidenceItem(
                        kind="mainai_task",
                        id=task.id,
                        occurred_at=when,  # type: ignore[arg-type]
                        title=(task.description or "")[:200],
                        status=getattr(task.status, "value", str(task.status)),
                        owner_id=owner_id,
                        source_table="mainai_tasks",
                        refs={"goal_id": str(goal.id), "plan_id": str(task.plan_id)},
                    )
                )
            events = db.execute(
                select(MainAITaskEvent).where(MainAITaskEvent.task_id == task.id)
            ).scalars().all()
            for event in events:
                if not in_range(event.created_at, rng):
                    continue
                items.append(
                    RecapEvidenceItem(
                        kind="mainai_task_event",
                        id=event.id,
                        occurred_at=event.created_at,
                        title=getattr(event.event_type, "value", str(event.event_type)),
                        status=None,
                        owner_id=owner_id,
                        source_table="mainai_task_events",
                        refs={"task_id": str(task.id)},
                    )
                )
    return items


def collect_work_candidates(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    for candidate in list_work_candidates(db, owner_id=owner_id):
        when = _ts(candidate.observed_at, candidate.created_at, candidate.updated_at)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="work_candidate",
                id=candidate.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=candidate.title or "",
                status=candidate.status,
                owner_id=owner_id,
                source_table="work_candidates",
                refs={"classifier_strategy": candidate.classifier_strategy},
            )
        )
    return items


def collect_project_entities(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    entities = db.execute(select(ProjectEntity).where(ProjectEntity.owner_id == owner_id)).scalars().all()
    for entity in entities:
        when = _ts(entity.decided_at, entity.updated_at, entity.created_at)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="project_entity",
                id=entity.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=entity.title or "",
                status=entity.status,
                owner_id=owner_id,
                source_table="project_entities",
                refs={
                    "entity_type": entity.entity_type,
                    "supersedes_entity_id": str(entity.supersedes_entity_id) if entity.supersedes_entity_id else None,
                },
            )
        )
    return items


def collect_engineering_lessons(db: Session, *, rng: TimeRange) -> list[RecapEvidenceItem]:
    """Founder-wide lessons (intentionally not owner-RLS)."""
    items: list[RecapEvidenceItem] = []
    lessons = db.execute(select(EngineeringLesson)).scalars().all()
    for lesson in lessons:
        when = _ts(lesson.first_seen_at, lesson.created_at)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="engineering_lesson",
                id=lesson.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=(lesson.problem or "")[:200],
                status=getattr(lesson.status, "value", str(lesson.status)),
                owner_id=None,
                source_table="engineering_lessons",
                refs={"affected_component": lesson.affected_component},
            )
        )
    return items


def collect_recovery(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    records = db.execute(
        select(MainAIRecoveryRecord).where(MainAIRecoveryRecord.owner_id == owner_id)
    ).scalars().all()
    for record in records:
        when = _ts(record.completed_at, record.detected_at, record.created_at)
        if in_range(when, rng):
            items.append(
                RecapEvidenceItem(
                    kind="mainai_recovery_record",
                    id=record.id,
                    occurred_at=when,  # type: ignore[arg-type]
                    title=getattr(record.classification, "value", str(getattr(record, "classification", ""))),
                    status=getattr(record.status, "value", str(getattr(record, "status", ""))),
                    owner_id=owner_id,
                    source_table="mainai_recovery_records",
                )
            )
        events = db.execute(
            select(MainAIRecoveryEvent).where(MainAIRecoveryEvent.recovery_record_id == record.id)
        ).scalars().all()
        for event in events:
            if not in_range(event.created_at, rng):
                continue
            items.append(
                RecapEvidenceItem(
                    kind="mainai_recovery_event",
                    id=event.id,
                    occurred_at=event.created_at,
                    title=getattr(event.event_type, "value", str(event.event_type)),
                    status=None,
                    owner_id=owner_id,
                    source_table="mainai_recovery_events",
                    refs={"recovery_record_id": str(record.id)},
                )
            )
    return items


def collect_memory_thread_events(db: Session, *, owner_id: uuid.UUID, rng: TimeRange) -> list[RecapEvidenceItem]:
    items: list[RecapEvidenceItem] = []
    events = db.execute(
        select(MemoryThreadEvent).where(MemoryThreadEvent.owner_id == owner_id)
    ).scalars().all()
    for event in events:
        if not in_range(event.created_at, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="memory_thread_event",
                id=event.id,
                occurred_at=event.created_at,
                title=getattr(event.event_type, "value", str(event.event_type)),
                status=None,
                owner_id=owner_id,
                source_table="memory_thread_events",
                refs={"thread_id": str(event.thread_id)},
            )
        )
    return items


def collect_project_memory_refs(db: Session, *, rng: TimeRange) -> list[RecapEvidenceItem]:
    """Founder-wide project sources / PR snapshots / checkpoints / notes (not owner-RLS)."""
    items: list[RecapEvidenceItem] = []
    for source in list_sources(db, limit=500):
        when = getattr(source, "ingested_at", None)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="project_source",
                id=source.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=f"{source.source_type}:{getattr(source, 'commit_sha', None) or source.id}",
                status=None,
                source_table="project_sources",
                refs={"source_type": source.source_type, "commit_sha": getattr(source, "commit_sha", None)},
            )
        )
    for snap in list_current_branch_pr_status(db):
        when = getattr(snap, "recorded_at", None)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="project_branch_pr_status",
                id=snap.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=f"{snap.kind}:{snap.ref}",
                status=snap.status,
                source_table="project_branch_pr_status",
                refs={"is_current": snap.is_current, "ci_status": snap.ci_status},
            )
        )
    for checkpoint in list_checkpoints(db, limit=200):
        when = getattr(checkpoint, "created_at", None)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="project_checkpoint",
                id=checkpoint.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=(checkpoint.summary or "checkpoint")[:200],
                status=None,
                source_table="project_checkpoints",
                refs={
                    "git_commit_sha": checkpoint.git_commit_sha,
                    "open_pr_refs": checkpoint.open_pr_refs,
                },
            )
        )
    for note in list_notes(db, status=None):
        when = getattr(note, "created_at", None)
        if not in_range(when, rng):
            continue
        items.append(
            RecapEvidenceItem(
                kind="project_note",
                id=note.id,
                occurred_at=when,  # type: ignore[arg-type]
                title=(note.content or "")[:200],
                status=getattr(note.status, "value", str(note.status)),
                source_table="project_notes",
                refs={"source_type": note.source_type, "source_ref": note.source_ref},
            )
        )
    return items


def all_collectors() -> Iterable:
    return (
        ("founder_memory", collect_founder_memory),
        ("goals_plans_tasks", collect_goals_plans_tasks),
        ("work_candidates", collect_work_candidates),
        ("project_entities", collect_project_entities),
        ("engineering_lessons", collect_engineering_lessons),
        ("recovery", collect_recovery),
        ("memory_thread_events", collect_memory_thread_events),
        ("project_memory_refs", collect_project_memory_refs),
    )
