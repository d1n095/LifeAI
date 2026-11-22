import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.document import Document
from app.models.project import Project, Task, TaskStatus
from app.models.user import User
from app.models.work_management import MilestoneStatus, TaskEvent, TaskEventType, WorkspaceMilestone
from app.schemas import (
    KnowledgeSourceOut,
    MilestoneIn,
    MilestoneOut,
    MilestoneUpdate,
    ProjectDetailOut,
    ProjectIn,
    ProjectOut,
    TaskDetailOut,
    TaskIn,
    TaskOut,
    TaskUpdate,
)
from app.work_management import (
    DependencyError,
    InvalidTaskTransitionError,
    MilestoneNotFoundError,
    ProjectNotFoundError,
    StaleTaskError,
    TaskNotFoundError,
    WorkManagementError,
    add_dependency,
    create_task,
    list_tasks as list_owned_tasks,
    milestone_out,
    owned_milestone,
    owned_project,
    owned_task,
    remove_dependency,
    task_detail,
    task_out,
    update_task,
)

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(require_founder)])


def _bad_request(exc: Exception) -> HTTPException:
    if isinstance(exc, StaleTaskError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (TaskNotFoundError, ProjectNotFoundError, MilestoneNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _owned_project_or_404(db: Session, project_id: uuid.UUID, user: User) -> Project:
    try:
        return owned_project(db, project_id, user.id)
    except ProjectNotFoundError as exc:
        raise _bad_request(exc) from exc


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return (
        db.query(Project)
        .filter(Project.created_by == user.id, Project.deleted_at.is_(None))
        .order_by(Project.updated_at.desc(), Project.id.asc())
        .all()
    )


@router.post("", response_model=ProjectOut)
def create_project(
    payload: ProjectIn, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    project = Project(**payload.model_dump(), created_by=user.id, last_activity_at=datetime.utcnow())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_project(
    project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    project = _owned_project_or_404(db, project_id, user)
    project.deleted_at = datetime.utcnow()
    project.version = (project.version or 1) + 1
    db.commit()
    return {"status": "deleted"}


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    project_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    priority: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    blocked_only: bool = Query(default=False),
    include_cancelled: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if project_id is not None:
        _owned_project_or_404(db, project_id, user)
    try:
        tasks = list_owned_tasks(
            db,
            owner_id=user.id,
            project_id=project_id,
            status=status_filter,
            priority=priority,
            q=q,
            blocked_only=blocked_only,
            include_cancelled=include_cancelled,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return [task_out(db, task, user.id) for task in tasks]


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task_endpoint(
    payload: TaskIn, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    try:
        task = create_task(
            db,
            owner_id=user.id,
            project_id=payload.project_id,
            title=payload.title,
            description=payload.description,
            status=payload.status,
            priority=payload.priority,
            due_at=payload.due_at,
            milestone_id=payload.milestone_id,
            depends_on_task_ids=payload.depends_on_task_ids,
        )
        db.commit()
        db.refresh(task)
        return task_out(db, task, user.id)
    except WorkManagementError as exc:
        db.rollback()
        raise _bad_request(exc) from exc


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
def get_task(
    task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    try:
        task = owned_task(db, task_id, user.id)
        return task_detail(db, task, user.id)
    except WorkManagementError as exc:
        raise _bad_request(exc) from exc


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task_endpoint(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        task = owned_task(db, task_id, user.id)
        fields = payload.model_dump(exclude_unset=True)
        expected_version = fields.pop("expected_version", None)
        depends = fields.pop("depends_on_task_ids", None)
        update_task(
            db,
            task=task,
            owner_id=user.id,
            fields=fields,
            expected_version=expected_version,
            depends_on_task_ids=depends,
        )
        db.commit()
        db.refresh(task)
        return task_out(db, task, user.id)
    except WorkManagementError as exc:
        db.rollback()
        raise _bad_request(exc) from exc


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    try:
        task = owned_task(db, task_id, user.id)
    except WorkManagementError as exc:
        raise _bad_request(exc) from exc
    task.deleted_at = datetime.utcnow()
    task.version = (task.version or 1) + 1
    db.add(
        TaskEvent(
            task_id=task.id,
            owner_id=user.id,
            event_type=TaskEventType.deleted.value,
            from_status=task.status.value if hasattr(task.status, "value") else str(task.status),
            detail={},
        )
    )
    db.commit()
    return {"status": "deleted"}


@router.post("/tasks/{task_id}/dependencies", response_model=TaskDetailOut)
def add_task_dependency(
    task_id: uuid.UUID,
    depends_on_task_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        task = owned_task(db, task_id, user.id)
        add_dependency(db, task=task, depends_on_id=depends_on_task_id, owner_id=user.id)
        db.commit()
        db.refresh(task)
        return task_detail(db, task, user.id)
    except WorkManagementError as exc:
        db.rollback()
        raise _bad_request(exc) from exc


@router.delete("/tasks/{task_id}/dependencies/{depends_on_task_id}", response_model=TaskDetailOut)
def remove_task_dependency(
    task_id: uuid.UUID,
    depends_on_task_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        task = owned_task(db, task_id, user.id)
        remove_dependency(
            db, task=task, depends_on_id=depends_on_task_id, owner_id=user.id
        )
        db.commit()
        db.refresh(task)
        return task_detail(db, task, user.id)
    except WorkManagementError as exc:
        db.rollback()
        raise _bad_request(exc) from exc


@router.get("/{project_id}/milestones", response_model=list[MilestoneOut])
def list_milestones(
    project_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    _owned_project_or_404(db, project_id, user)
    rows = (
        db.query(WorkspaceMilestone)
        .filter(
            WorkspaceMilestone.project_id == project_id,
            WorkspaceMilestone.owner_id == user.id,
        )
        .order_by(
            WorkspaceMilestone.due_at.asc().nulls_last(),
            WorkspaceMilestone.created_at.asc(),
            WorkspaceMilestone.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [milestone_out(db, row, user.id) for row in rows]


@router.post("/{project_id}/milestones", response_model=MilestoneOut, status_code=status.HTTP_201_CREATED)
def create_milestone(
    project_id: uuid.UUID,
    payload: MilestoneIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    _owned_project_or_404(db, project_id, user)
    milestone = WorkspaceMilestone(
        project_id=project_id,
        owner_id=user.id,
        title=payload.title,
        description=payload.description,
        due_at=payload.due_at,
        status=MilestoneStatus(payload.status),
        completed_at=datetime.utcnow() if payload.status == "completed" else None,
    )
    db.add(milestone)
    db.commit()
    db.refresh(milestone)
    return milestone_out(db, milestone, user.id)


@router.patch("/milestones/{milestone_id}", response_model=MilestoneOut)
def update_milestone(
    milestone_id: uuid.UUID,
    payload: MilestoneUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        milestone = owned_milestone(db, milestone_id, user.id)
    except WorkManagementError as exc:
        raise _bad_request(exc) from exc
    fields = payload.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version", None)
    if expected_version is not None and expected_version != (milestone.version or 1):
        raise HTTPException(status_code=409, detail="Milstolpen har ändrats sedan den lästes.")
    for key, value in fields.items():
        if key == "status":
            milestone.status = MilestoneStatus(value)
            milestone.completed_at = datetime.utcnow() if value == "completed" else None
        else:
            setattr(milestone, key, value)
    milestone.version = (milestone.version or 1) + 1
    db.commit()
    db.refresh(milestone)
    return milestone_out(db, milestone, user.id)


@router.delete("/milestones/{milestone_id}")
def delete_milestone(
    milestone_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    try:
        milestone = owned_milestone(db, milestone_id, user.id)
    except WorkManagementError as exc:
        raise _bad_request(exc) from exc
    db.query(Task).filter(
        Task.milestone_id == milestone.id, Task.created_by == user.id
    ).update({Task.milestone_id: None}, synchronize_session=False)
    db.delete(milestone)
    db.commit()
    return {"status": "deleted"}


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(
    project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)
):
    """One project plus only the tasks, milestones and sources that belong to it.

    Every collection is owner-scoped and filtered server-side; the frontend never derives
    project membership from a global list.
    """
    project = _owned_project_or_404(db, project_id, user)
    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.created_by == user.id,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at.asc(), Task.id.asc())
        .all()
    )
    milestones = (
        db.query(WorkspaceMilestone)
        .filter(
            WorkspaceMilestone.project_id == project_id,
            WorkspaceMilestone.owner_id == user.id,
        )
        .order_by(WorkspaceMilestone.due_at.asc().nulls_last(), WorkspaceMilestone.created_at.asc())
        .all()
    )
    sources = (
        db.query(Document)
        .filter(
            Document.project_id == project_id,
            Document.uploaded_by == user.id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc(), Document.id.asc())
        .all()
    )
    return ProjectDetailOut(
        **ProjectOut.model_validate(project).model_dump(),
        tasks=[task_out(db, task, user.id) for task in tasks],
        milestones=[milestone_out(db, item, user.id) for item in milestones],
        sources=[KnowledgeSourceOut.model_validate(source) for source in sources],
    )
