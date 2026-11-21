import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.document import Document
from app.models.project import Project, Task
from app.models.user import User
from app.schemas import KnowledgeSourceOut, ProjectDetailOut, ProjectIn, ProjectOut, TaskIn, TaskOut

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(require_founder)])


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.created_at.desc()).all()


@router.post("", response_model=ProjectOut)
def create_project(payload: ProjectIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = Project(**payload.model_dump(), created_by=user.id)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_project(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Projektet hittades inte.")
    db.delete(project)
    db.commit()
    return {"status": "deleted"}


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.created_at.desc()).all()


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """One project plus only the tasks and knowledge sources that belong to it.

    The source query is owner-scoped and excludes soft-deleted rows explicitly. RLS already
    scopes ordinary document reads, but this endpoint must not rely on the UI to filter a
    global library response: a project page that showed another project's source would be a
    real cross-project disclosure even if the underlying row remained owner-scoped.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Projektet hittades inte.")

    tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.created_at.asc())
        .all()
    )
    sources = (
        db.query(Document)
        .filter(
            Document.project_id == project_id,
            Document.uploaded_by == user.id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
        .all()
    )

    return ProjectDetailOut(
        **ProjectOut.model_validate(project).model_dump(),
        tasks=[TaskOut.model_validate(task) for task in tasks],
        sources=[KnowledgeSourceOut.model_validate(source) for source in sources],
    )


@router.post("/tasks", response_model=TaskOut)
def create_task(payload: TaskIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    task = Task(**payload.model_dump(), created_by=user.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(task_id: uuid.UUID, payload: TaskIn, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Uppgiften hittades inte.")
    for key, value in payload.model_dump().items():
        setattr(task, key, value)
    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}")
def delete_task(task_id: uuid.UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Uppgiften hittades inte.")
    db.delete(task)
    db.commit()
    return {"status": "deleted"}
