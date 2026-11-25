"""Founder task/milestone domain logic.

This module deliberately operates on the existing `tasks` table and the additive
`workspace_milestones`/`task_dependencies`/`task_events` tables. It does not dispatch, approve
or execute anything; a founder task is never a MainAI execution job.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.project import Project, Task, TaskStatus
from app.models.work_management import (
    MilestoneStatus,
    TaskDependency,
    TaskEvent,
    TaskEventType,
    WorkspaceMilestone,
)
from app.schemas import MilestoneOut, TaskDependencyOut, TaskDetailOut, TaskEventOut, TaskOut


class WorkManagementError(ValueError):
    pass


class TaskNotFoundError(WorkManagementError):
    pass


class MilestoneNotFoundError(WorkManagementError):
    pass


class ProjectNotFoundError(WorkManagementError):
    pass


class StaleTaskError(WorkManagementError):
    pass


class InvalidTaskTransitionError(WorkManagementError):
    pass


class DependencyError(WorkManagementError):
    pass


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "todo": {"in_progress", "blocked", "done", "cancelled"},
    "in_progress": {"todo", "blocked", "done", "cancelled"},
    "blocked": {"todo", "in_progress", "cancelled"},
    "done": {"todo", "in_progress"},
    "cancelled": {"todo"},
}
_TERMINAL = {"done", "cancelled"}
_DEPENDENCY_BLOCK_PREFIX = "Väntar på: "


def owned_project(db: Session, project_id: uuid.UUID, owner_id: uuid.UUID) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.created_by == owner_id,
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None:
        raise ProjectNotFoundError("Projektet hittades inte.")
    return project


def owned_task(
    db: Session, task_id: uuid.UUID, owner_id: uuid.UUID, *, include_deleted: bool = False
) -> Task:
    query = db.query(Task).filter(Task.id == task_id, Task.created_by == owner_id)
    if not include_deleted:
        query = query.filter(Task.deleted_at.is_(None))
    task = query.first()
    if task is None:
        raise TaskNotFoundError("Uppgiften hittades inte.")
    return task


def owned_milestone(db: Session, milestone_id: uuid.UUID, owner_id: uuid.UUID) -> WorkspaceMilestone:
    milestone = (
        db.query(WorkspaceMilestone)
        .filter(WorkspaceMilestone.id == milestone_id, WorkspaceMilestone.owner_id == owner_id)
        .first()
    )
    if milestone is None:
        raise MilestoneNotFoundError("Milstolpen hittades inte.")
    return milestone


def _status(task: Task) -> str:
    return task.status.value if hasattr(task.status, "value") else str(task.status)


def _priority(task: Task) -> str:
    return task.priority.value if hasattr(task.priority, "value") else str(task.priority)


def _record_event(
    db: Session,
    *,
    task: Task,
    owner_id: uuid.UUID,
    event_type: TaskEventType,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: dict | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        owner_id=owner_id,
        event_type=event_type.value,
        from_status=from_status,
        to_status=to_status,
        detail=detail or {},
    )
    db.add(event)
    return event


def _dependency_rows(db: Session, task_id: uuid.UUID, owner_id: uuid.UUID) -> list[TaskDependency]:
    return (
        db.query(TaskDependency)
        .filter(TaskDependency.task_id == task_id, TaskDependency.owner_id == owner_id)
        .order_by(TaskDependency.created_at.asc(), TaskDependency.id.asc())
        .all()
    )


def dependency_count(db: Session, task_id: uuid.UUID, owner_id: uuid.UUID) -> int:
    return (
        db.query(func.count(TaskDependency.id))
        .filter(TaskDependency.task_id == task_id, TaskDependency.owner_id == owner_id)
        .scalar()
        or 0
    )


def _blocking_tasks(db: Session, task: Task, owner_id: uuid.UUID) -> list[Task]:
    return (
        db.query(Task)
        .join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
        .filter(
            TaskDependency.task_id == task.id,
            TaskDependency.owner_id == owner_id,
            Task.created_by == owner_id,
            Task.deleted_at.is_(None),
            Task.status.notin_([TaskStatus.done, TaskStatus.cancelled]),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .all()
    )


def is_blocked(db: Session, task: Task, owner_id: uuid.UUID) -> bool:
    if _status(task) in _TERMINAL:
        return False
    return bool(_blocking_tasks(db, task, owner_id)) or _status(task) == "blocked"


def _set_dependency_block(db: Session, task: Task, owner_id: uuid.UUID) -> bool:
    """Apply dependency-derived blocking without erasing a founder's own block reason."""

    if _status(task) in _TERMINAL:
        return False
    blockers = _blocking_tasks(db, task, owner_id)
    current = _status(task)
    if blockers:
        names = ", ".join(blocker.title for blocker in blockers[:4])
        reason = f"{_DEPENDENCY_BLOCK_PREFIX}{names}"
        if current != "blocked" or task.blocked_reason != reason:
            from_status = current
            task.status = TaskStatus.blocked
            task.blocked_reason = reason[:512]
            task.version = (task.version or 1) + 1
            _record_event(
                db,
                task=task,
                owner_id=owner_id,
                event_type=TaskEventType.blocked,
                from_status=from_status,
                to_status="blocked",
                detail={"blocking_task_ids": [str(item.id) for item in blockers]},
            )
            return True
        return False
    if current == "blocked" and (task.blocked_reason or "").startswith(_DEPENDENCY_BLOCK_PREFIX):
        task.status = TaskStatus.todo
        task.blocked_reason = None
        task.version = (task.version or 1) + 1
        _record_event(
            db,
            task=task,
            owner_id=owner_id,
            event_type=TaskEventType.unblocked,
            from_status="blocked",
            to_status="todo",
            detail={},
        )
        return True
    return False


def _recompute_dependents(db: Session, task: Task, owner_id: uuid.UUID) -> None:
    rows = (
        db.query(TaskDependency)
        .filter(
            TaskDependency.depends_on_task_id == task.id,
            TaskDependency.owner_id == owner_id,
        )
        .all()
    )
    for row in rows:
        dependent = (
            db.query(Task)
            .filter(
                Task.id == row.task_id,
                Task.created_by == owner_id,
                Task.deleted_at.is_(None),
            )
            .first()
        )
        if dependent is not None:
            _set_dependency_block(db, dependent, owner_id)


def _would_cycle(db: Session, task_id: uuid.UUID, depends_on_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
    """Depth-first walk of the existing prerequisite graph."""

    seen: set[uuid.UUID] = set()
    stack = [depends_on_id]
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(
            row.depends_on_task_id
            for row in db.query(TaskDependency)
            .filter(TaskDependency.task_id == current, TaskDependency.owner_id == owner_id)
            .all()
        )
    return False


def _validate_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTaskTransitionError(f"Ogiltig statusövergång: {current} → {target}.")


def _validate_project_and_milestone(
    db: Session,
    *,
    owner_id: uuid.UUID,
    project_id: uuid.UUID | None,
    milestone_id: uuid.UUID | None,
) -> None:
    if project_id is not None:
        owned_project(db, project_id, owner_id)
    if milestone_id is not None:
        milestone = owned_milestone(db, milestone_id, owner_id)
        if project_id is not None and milestone.project_id != project_id:
            raise WorkManagementError("Milstolpen tillhör ett annat projekt.")


def _add_dependency(
    db: Session,
    *,
    task: Task,
    depends_on: Task,
    owner_id: uuid.UUID,
    record_event: bool = True,
) -> TaskDependency:
    if task.id == depends_on.id:
        raise DependencyError("En uppgift kan inte vara beroende av sig själv.")
    if depends_on.created_by != owner_id:
        raise TaskNotFoundError("Beroendets uppgift hittades inte.")
    if task.project_id and depends_on.project_id and task.project_id != depends_on.project_id:
        raise DependencyError("Beroenden måste tillhöra samma projekt.")
    existing = (
        db.query(TaskDependency)
        .filter(
            TaskDependency.task_id == task.id,
            TaskDependency.depends_on_task_id == depends_on.id,
            TaskDependency.owner_id == owner_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    if _would_cycle(db, task.id, depends_on.id, owner_id):
        raise DependencyError("Beroendet skulle skapa en cykel.")
    row = TaskDependency(
        task_id=task.id,
        depends_on_task_id=depends_on.id,
        owner_id=owner_id,
    )
    db.add(row)
    db.flush()
    if record_event:
        _record_event(
            db,
            task=task,
            owner_id=owner_id,
            event_type=TaskEventType.dependency_added,
            detail={"depends_on_task_id": str(depends_on.id), "depends_on_title": depends_on.title},
        )
    _set_dependency_block(db, task, owner_id)
    return row


def create_task(
    db: Session,
    *,
    owner_id: uuid.UUID,
    project_id: uuid.UUID | None,
    title: str,
    description: str | None,
    status: str,
    priority: str,
    due_at: datetime | None,
    milestone_id: uuid.UUID | None,
    depends_on_task_ids: list[uuid.UUID],
) -> Task:
    _validate_project_and_milestone(
        db, owner_id=owner_id, project_id=project_id, milestone_id=milestone_id
    )
    if status not in _ALLOWED_TRANSITIONS:
        raise InvalidTaskTransitionError(f"Okänd status: {status}.")
    task = Task(
        created_by=owner_id,
        project_id=project_id,
        title=title,
        description=description,
        status=TaskStatus(status),
        priority=priority,
        due_at=due_at,
        milestone_id=milestone_id,
        completed_at=datetime.utcnow() if status == "done" else None,
        blocked_reason=None,
        version=1,
    )
    db.add(task)
    db.flush()
    _record_event(
        db,
        task=task,
        owner_id=owner_id,
        event_type=TaskEventType.created,
        to_status=status,
        detail={"project_id": str(project_id) if project_id else None},
    )
    for depends_on_id in dict.fromkeys(depends_on_task_ids):
        depends_on = owned_task(db, depends_on_id, owner_id)
        _add_dependency(db, task=task, depends_on=depends_on, owner_id=owner_id)
    _set_dependency_block(db, task, owner_id)
    _recompute_milestone(db, milestone_id, owner_id) if milestone_id else None
    db.flush()
    return task


def update_task(
    db: Session,
    *,
    task: Task,
    owner_id: uuid.UUID,
    fields: dict,
    expected_version: int | None,
    depends_on_task_ids: list[uuid.UUID] | None,
) -> Task:
    if expected_version is not None and expected_version != (task.version or 1):
        raise StaleTaskError("Uppgiften har ändrats sedan den lästes. Läs om och försök igen.")
    old_status = _status(task)
    if "project_id" in fields:
        _validate_project_and_milestone(
            db,
            owner_id=owner_id,
            project_id=fields["project_id"],
            milestone_id=fields.get("milestone_id", task.milestone_id),
        )
    elif "milestone_id" in fields:
        _validate_project_and_milestone(
            db,
            owner_id=owner_id,
            project_id=task.project_id,
            milestone_id=fields["milestone_id"],
        )
    if "status" in fields:
        _validate_transition(old_status, fields["status"])
        if fields["status"] in ("done", "cancelled") and task.completed_at is None:
            task.completed_at = datetime.utcnow()
        if fields["status"] not in ("done", "cancelled"):
            task.completed_at = None

    old_milestone = task.milestone_id
    for key in (
        "project_id",
        "title",
        "description",
        "status",
        "priority",
        "due_at",
        "milestone_id",
        "blocked_reason",
    ):
        if key in fields:
            setattr(task, key, fields[key])
    task.version = (task.version or 1) + 1
    db.flush()
    new_status = _status(task)
    _record_event(
        db,
        task=task,
        owner_id=owner_id,
        event_type=TaskEventType.status_changed if old_status != new_status else TaskEventType.updated,
        from_status=old_status,
        to_status=new_status,
        detail={"changed_fields": sorted(fields.keys())},
    )

    if depends_on_task_ids is not None:
        existing = {row.depends_on_task_id: row for row in _dependency_rows(db, task.id, owner_id)}
        desired = set(dict.fromkeys(depends_on_task_ids))
        for dep_id, row in existing.items():
            if dep_id not in desired:
                db.delete(row)
                _record_event(
                    db,
                    task=task,
                    owner_id=owner_id,
                    event_type=TaskEventType.dependency_removed,
                    detail={"depends_on_task_id": str(dep_id)},
                )
        for dep_id in desired:
            if dep_id not in existing:
                depends_on = owned_task(db, dep_id, owner_id)
                _add_dependency(db, task=task, depends_on=depends_on, owner_id=owner_id)

    _set_dependency_block(db, task, owner_id)
    _recompute_dependents(db, task, owner_id)
    if old_milestone and old_milestone != task.milestone_id:
        _recompute_milestone(db, old_milestone, owner_id)
    if task.milestone_id:
        _recompute_milestone(db, task.milestone_id, owner_id)
    db.flush()
    return task


def add_dependency(
    db: Session, *, task: Task, depends_on_id: uuid.UUID, owner_id: uuid.UUID
) -> TaskDependency:
    depends_on = owned_task(db, depends_on_id, owner_id)
    return _add_dependency(db, task=task, depends_on=depends_on, owner_id=owner_id)


def remove_dependency(db: Session, *, task: Task, depends_on_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    row = (
        db.query(TaskDependency)
        .filter(
            TaskDependency.task_id == task.id,
            TaskDependency.depends_on_task_id == depends_on_id,
            TaskDependency.owner_id == owner_id,
        )
        .first()
    )
    if row is None:
        raise DependencyError("Beroendet finns inte.")
    db.delete(row)
    _record_event(
        db,
        task=task,
        owner_id=owner_id,
        event_type=TaskEventType.dependency_removed,
        detail={"depends_on_task_id": str(depends_on_id)},
    )
    task.version = (task.version or 1) + 1
    _set_dependency_block(db, task, owner_id)
    _recompute_dependents(db, task, owner_id)
    db.flush()


def _recompute_milestone(db: Session, milestone_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    milestone = (
        db.query(WorkspaceMilestone)
        .filter(WorkspaceMilestone.id == milestone_id, WorkspaceMilestone.owner_id == owner_id)
        .first()
    )
    if milestone is None or milestone.status == MilestoneStatus.cancelled:
        return
    tasks = (
        db.query(Task)
        .filter(
            Task.milestone_id == milestone.id,
            Task.created_by == owner_id,
            Task.deleted_at.is_(None),
        )
        .all()
    )
    if not tasks:
        return
    done = all(_status(task) in ("done", "cancelled") for task in tasks)
    if done and milestone.status != MilestoneStatus.completed:
        milestone.status = MilestoneStatus.completed
        milestone.completed_at = datetime.utcnow()
        milestone.version = (milestone.version or 1) + 1
    elif not done and milestone.status == MilestoneStatus.completed:
        milestone.status = MilestoneStatus.in_progress
        milestone.completed_at = None
        milestone.version = (milestone.version or 1) + 1


def task_out(db: Session, task: Task, owner_id: uuid.UUID) -> TaskOut:
    out = TaskOut.model_validate(task)
    out.is_blocked = is_blocked(db, task, owner_id)
    out.dependency_count = dependency_count(db, task.id, owner_id)
    return out


def task_outs(db: Session, tasks: list[Task], owner_id: uuid.UUID) -> list[TaskOut]:
    """Serialize a bounded task page with one dependency query, never one query per row."""

    if not tasks:
        return []
    ids = [task.id for task in tasks]
    rows = (
        db.query(TaskDependency.task_id, TaskDependency.depends_on_task_id, Task.status)
        .join(Task, Task.id == TaskDependency.depends_on_task_id)
        .filter(
            TaskDependency.task_id.in_(ids),
            TaskDependency.owner_id == owner_id,
            Task.created_by == owner_id,
            Task.deleted_at.is_(None),
        )
        .all()
    )
    counts = {task_id: 0 for task_id in ids}
    blocked: set[uuid.UUID] = set()
    for task_id, _depends_on, dep_status in rows:
        counts[task_id] = counts.get(task_id, 0) + 1
        status_value = dep_status.value if hasattr(dep_status, "value") else str(dep_status)
        if status_value not in ("done", "cancelled"):
            blocked.add(task_id)
    result: list[TaskOut] = []
    for task in tasks:
        out = TaskOut.model_validate(task)
        out.dependency_count = counts.get(task.id, 0)
        out.is_blocked = task.id in blocked or (
            task.status.value if hasattr(task.status, "value") else str(task.status)
        ) == "blocked"
        result.append(out)
    return result


def milestone_out(db: Session, milestone: WorkspaceMilestone, owner_id: uuid.UUID) -> MilestoneOut:
    rows = (
        db.query(Task.status)
        .filter(
            Task.milestone_id == milestone.id,
            Task.created_by == owner_id,
            Task.deleted_at.is_(None),
        )
        .all()
    )
    statuses = [row[0].value if hasattr(row[0], "value") else str(row[0]) for row in rows]
    return MilestoneOut(
        **{
            "id": milestone.id,
            "project_id": milestone.project_id,
            "title": milestone.title,
            "description": milestone.description,
            "due_at": milestone.due_at,
            "status": milestone.status.value if hasattr(milestone.status, "value") else milestone.status,
            "completed_at": milestone.completed_at,
            "version": milestone.version,
            "created_at": milestone.created_at,
            "updated_at": milestone.updated_at,
            "task_count": len(statuses),
            "completed_task_count": sum(1 for status in statuses if status in ("done", "cancelled")),
        }
    )


def task_detail(db: Session, task: Task, owner_id: uuid.UUID) -> TaskDetailOut:
    dependencies = (
        db.query(TaskDependency, Task)
        .join(Task, Task.id == TaskDependency.depends_on_task_id)
        .filter(TaskDependency.task_id == task.id, TaskDependency.owner_id == owner_id)
        .order_by(TaskDependency.created_at.asc(), TaskDependency.id.asc())
        .all()
    )
    dependents = (
        db.query(TaskDependency, Task)
        .join(Task, Task.id == TaskDependency.task_id)
        .filter(TaskDependency.depends_on_task_id == task.id, TaskDependency.owner_id == owner_id)
        .order_by(TaskDependency.created_at.asc(), TaskDependency.id.asc())
        .all()
    )
    events = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.owner_id == owner_id)
        .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
        .all()
    )
    milestone = (
        db.query(WorkspaceMilestone)
        .filter(WorkspaceMilestone.id == task.milestone_id, WorkspaceMilestone.owner_id == owner_id)
        .first()
        if task.milestone_id
        else None
    )
    project = db.query(Project).filter(Project.id == task.project_id).first() if task.project_id else None
    base = task_out(db, task, owner_id)
    return TaskDetailOut(
        **base.model_dump(),
        dependencies=[
            TaskDependencyOut(
                id=row.id,
                task_id=row.task_id,
                depends_on_task_id=row.depends_on_task_id,
                depends_on_title=dep.title,
                depends_on_status=dep.status.value if hasattr(dep.status, "value") else str(dep.status),
            )
            for row, dep in dependencies
        ],
        dependents=[
            TaskDependencyOut(
                id=row.id,
                task_id=row.task_id,
                depends_on_task_id=row.depends_on_task_id,
                depends_on_title=dep.title,
                depends_on_status=dep.status.value if hasattr(dep.status, "value") else str(dep.status),
            )
            for row, dep in dependents
        ],
        events=[TaskEventOut.model_validate(event) for event in events],
        milestone=milestone_out(db, milestone, owner_id) if milestone else None,
        project_name=project.name if project else None,
    )


def list_tasks(
    db: Session,
    *,
    owner_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    blocked_only: bool = False,
    include_cancelled: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[Task]:
    query = db.query(Task).filter(Task.created_by == owner_id, Task.deleted_at.is_(None))
    if project_id is not None:
        query = query.filter(Task.project_id == project_id)
    if status is not None:
        query = query.filter(Task.status == TaskStatus(status))
    if priority is not None:
        query = query.filter(Task.priority == priority)
    if q:
        query = query.filter(Task.title.ilike(f"%{q.strip()}%"))
    if not include_cancelled:
        query = query.filter(Task.status != TaskStatus.cancelled)
    rows = (
        query.order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc(), Task.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if blocked_only:
        rows = [task for task in rows if is_blocked(db, task, owner_id)]
    return rows
