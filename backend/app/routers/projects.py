import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.project import Project, Task
from app.models.user import User
from app.schemas import ProjectIn, ProjectOut, TaskIn, TaskOut

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.created_at.desc()).all()


@router.post("", response_model=ProjectOut)
def create_project(payload: ProjectIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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


@router.post("/tasks", response_model=TaskOut)
def create_task(payload: TaskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
