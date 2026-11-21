"""Life Workspaces API.

A workspace is the existing founder project row plus the missing product composition:
purpose/goals, archive/restore, owner-scoped activity and decisions, workspace-linked
conversations, and a bounded context view assembled from the existing document, task,
conversation and retrieval systems. No second project model, chat engine or authorization
system is introduced here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.models.conversation import Conversation
from app.models.document import ActiveTruthStatus, Document
from app.models.project import Project, Task
from app.models.user import User
from app.models.workspace import (
    WorkspaceActivity,
    WorkspaceDecision,
    WorkspaceDecisionStatus,
    WorkspaceMember,
    WorkspaceMemberRole,
)
from app.providers.base import ProviderError
from app.rag.retrieve import GroundingUnavailableError, retrieve_context
from app.schemas import (
    ConversationOut,
    KnowledgeSourceOut,
    TaskIn,
    TaskOut,
    WorkspaceActivityOut,
    WorkspaceContextOut,
    WorkspaceContextSourceOut,
    WorkspaceDecisionIn,
    WorkspaceDecisionOut,
    WorkspaceDetailOut,
    WorkspaceIn,
    WorkspaceOut,
    WorkspaceUpdate,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"], dependencies=[Depends(require_founder)])


def _owned_project(db: Session, project_id: uuid.UUID, user: User) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Workspace hittades inte.")
    return project


def _record_activity(
    db: Session,
    *,
    project: Project,
    user: User,
    kind: str,
    summary: str,
    detail: dict | None = None,
) -> WorkspaceActivity:
    activity = WorkspaceActivity(
        project_id=project.id,
        owner_id=user.id,
        kind=kind,
        summary=summary[:512],
        detail=detail or {},
    )
    project.last_activity_at = datetime.utcnow()
    db.add(activity)
    return activity


def _workspace_sources(db: Session, project: Project, user: User, limit: int = 50) -> list[Document]:
    return (
        db.query(Document)
        .filter(
            Document.project_id == project.id,
            Document.uploaded_by == user.id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc(), Document.id.asc())
        .limit(limit)
        .all()
    )


def _workspace_tasks(db: Session, project: Project, user: User, limit: int = 100) -> list[Task]:
    return (
        db.query(Task)
        .filter(Task.project_id == project.id, Task.created_by == user.id)
        .order_by(Task.created_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _workspace_conversations(db: Session, project: Project, user: User, limit: int = 20) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter(Conversation.project_id == project.id, Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.asc())
        .limit(limit)
        .all()
    )


def _workspace_decisions(db: Session, project: Project, user: User, limit: int = 50) -> list[WorkspaceDecision]:
    return (
        db.query(WorkspaceDecision)
        .filter(WorkspaceDecision.project_id == project.id, WorkspaceDecision.owner_id == user.id)
        .order_by(WorkspaceDecision.created_at.desc(), WorkspaceDecision.id.asc())
        .limit(limit)
        .all()
    )


def _workspace_activities(db: Session, project: Project, user: User, limit: int = 50) -> list[WorkspaceActivity]:
    return (
        db.query(WorkspaceActivity)
        .filter(WorkspaceActivity.project_id == project.id, WorkspaceActivity.owner_id == user.id)
        .order_by(WorkspaceActivity.created_at.desc(), WorkspaceActivity.id.asc())
        .limit(limit)
        .all()
    )


async def _build_context(
    db: Session,
    *,
    project: Project,
    user: User,
    query: str | None = None,
) -> WorkspaceContextOut:
    sources = _workspace_sources(db, project, user, limit=20)
    tasks = _workspace_tasks(db, project, user, limit=50)
    conversations = _workspace_conversations(db, project, user, limit=10)
    decisions = _workspace_decisions(db, project, user, limit=10)
    activities = _workspace_activities(db, project, user, limit=10)

    objectives = [item for item in [project.purpose, project.description] if item]
    objectives.extend(project.goals or [])

    uncertainties: list[dict] = []
    for source in sources:
        if source.active_truth_status in (
            ActiveTruthStatus.proposed,
            ActiveTruthStatus.disputed,
            ActiveTruthStatus.superseded,
        ):
            uncertainties.append(
                {
                    "kind": "source_status",
                    "document_id": str(source.id),
                    "title": source.title,
                    "status": source.active_truth_status.value,
                }
            )
    for decision in decisions:
        if decision.status == WorkspaceDecisionStatus.proposed:
            uncertainties.append(
                {
                    "kind": "proposed_decision",
                    "decision_id": str(decision.id),
                    "title": decision.title,
                    "status": decision.status.value,
                }
            )

    retrieval_sources: list[WorkspaceContextSourceOut] = []
    degraded_reason: str | None = None
    if query and query.strip():
        try:
            hits = await retrieve_context(
                db,
                user.id,
                query.strip(),
                top_k=8,
                project_id=project.id,
                effect_guard=True,
            )
            retrieval_sources = [
                WorkspaceContextSourceOut(
                    document_id=uuid.UUID(str(hit["document_id"])),
                    title=hit["title"],
                    snippet=hit["text"][:300],
                    score=hit["score"],
                    active_truth_status=hit.get("active_truth_status"),
                )
                for hit in hits
            ]
        except (GroundingUnavailableError, EgressDeniedError, ProviderError) as exc:
            # Context assembly is useful even when semantic retrieval is unavailable. The
            # founder gets the deterministic workspace facts plus an explicit degraded reason.
            degraded_reason = str(exc)

    return WorkspaceContextOut(
        workspace_id=project.id,
        objectives=objectives,
        source_documents=[KnowledgeSourceOut.model_validate(source) for source in sources],
        decisions=[WorkspaceDecisionOut.model_validate(decision) for decision in decisions],
        outstanding_tasks=[TaskOut.model_validate(task) for task in tasks if task.status != "done"],
        past_conversations=[ConversationOut.model_validate(conversation) for conversation in conversations],
        recent_changes=[WorkspaceActivityOut.model_validate(activity) for activity in activities],
        uncertainties=uncertainties,
        retrieval_sources=retrieval_sources,
        retrieval_degraded_reason=degraded_reason,
    )


def _detail(db: Session, project: Project, user: User, context: WorkspaceContextOut) -> WorkspaceDetailOut:
    return WorkspaceDetailOut(
        **WorkspaceOut.model_validate(project).model_dump(),
        tasks=[TaskOut.model_validate(task) for task in _workspace_tasks(db, project, user)],
        sources=[KnowledgeSourceOut.model_validate(source) for source in _workspace_sources(db, project, user)],
        conversations=[
            ConversationOut.model_validate(conversation)
            for conversation in _workspace_conversations(db, project, user)
        ],
        activities=[
            WorkspaceActivityOut.model_validate(activity)
            for activity in _workspace_activities(db, project, user)
        ],
        decisions=[
            WorkspaceDecisionOut.model_validate(decision)
            for decision in _workspace_decisions(db, project, user)
        ],
        context=context,
    )


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    query = db.query(Project).filter(Project.created_by == user.id, Project.deleted_at.is_(None))
    if not include_archived:
        query = query.filter(Project.archived_at.is_(None))
    return query.order_by(Project.updated_at.desc(), Project.id.asc()).all()


@router.post("", response_model=WorkspaceOut)
def create_workspace(payload: WorkspaceIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = Project(
        created_by=user.id,
        name=payload.name,
        description=payload.description,
        purpose=payload.purpose,
        goals=payload.goals,
        status=payload.status,
        settings=payload.settings,
        last_activity_at=datetime.utcnow(),
    )
    db.add(project)
    db.flush()
    db.add(WorkspaceMember(project_id=project.id, user_id=user.id, role=WorkspaceMemberRole.owner))
    _record_activity(
        db,
        project=project,
        user=user,
        kind="workspace.created",
        summary=f"Workspace skapades: {project.name}",
    )
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=WorkspaceDetailOut)
async def get_workspace(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    context = await _build_context(db, project=project, user=user)
    return _detail(db, project, user, context)


@router.patch("/{project_id}", response_model=WorkspaceOut)
def update_workspace(
    project_id: uuid.UUID,
    payload: WorkspaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    changed = payload.model_dump(exclude_unset=True)
    for key, value in changed.items():
        setattr(project, key, value)
    if changed:
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.updated",
            summary="Workspace-metadata uppdaterades",
            detail={"fields": sorted(changed)},
        )
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/archive", response_model=WorkspaceOut)
def archive_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    if project.archived_at is None:
        project.archived_at = datetime.utcnow()
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.archived",
            summary="Workspace arkiverades",
        )
        db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/restore", response_model=WorkspaceOut)
def restore_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    if project.archived_at is not None:
        project.archived_at = None
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.restored",
            summary="Workspace återställdes",
        )
        db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    project.deleted_at = datetime.utcnow()
    project.archived_at = project.archived_at or datetime.utcnow()
    _record_activity(
        db,
        project=project,
        user=user,
        kind="workspace.deleted",
        summary="Workspace soft-raderades",
    )
    db.commit()
    return {"status": "deleted", "retained": True}


@router.get("/{project_id}/knowledge", response_model=list[KnowledgeSourceOut])
def list_workspace_knowledge(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [KnowledgeSourceOut.model_validate(source) for source in _workspace_sources(db, project, user)]


@router.post("/{project_id}/documents/{document_id}", response_model=KnowledgeSourceOut)
def associate_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.uploaded_by == user.id, Document.deleted_at.is_(None))
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")
    previous = document.project_id
    document.project_id = project.id
    _record_activity(
        db,
        project=project,
        user=user,
        kind="document.associated",
        summary=f"Källa knöts till workspace: {document.title}",
        detail={"document_id": str(document.id), "previous_project_id": str(previous) if previous else None},
    )
    db.commit()
    db.refresh(document)
    return document


@router.delete("/{project_id}/documents/{document_id}", response_model=KnowledgeSourceOut)
def disassociate_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            Document.uploaded_by == user.id,
            Document.project_id == project.id,
            Document.deleted_at.is_(None),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Källan finns inte i det här workspaceet.")
    document.project_id = None
    _record_activity(
        db,
        project=project,
        user=user,
        kind="document.disassociated",
        summary=f"Källa kopplades loss: {document.title}",
        detail={"document_id": str(document.id)},
    )
    db.commit()
    db.refresh(document)
    return document


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
def list_workspace_tasks(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [TaskOut.model_validate(task) for task in _workspace_tasks(db, project, user)]


@router.post("/{project_id}/tasks", response_model=TaskOut)
def create_workspace_task(
    project_id: uuid.UUID,
    payload: TaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    task = Task(
        **payload.model_dump(exclude={"project_id"}),
        project_id=project.id,
        created_by=user.id,
    )
    db.add(task)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="task.created",
        summary=f"Uppgift skapades: {task.title}",
        detail={"task_id": str(task.id)},
    )
    db.commit()
    db.refresh(task)
    return task


@router.get("/{project_id}/conversations", response_model=list[ConversationOut])
def list_workspace_conversations(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [ConversationOut.model_validate(conversation) for conversation in _workspace_conversations(db, project, user)]


@router.post("/{project_id}/conversations", response_model=ConversationOut)
def create_workspace_conversation(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    conversation = Conversation(
        user_id=user.id,
        project_id=project.id,
        title=f"Workspace: {project.name}"[:256],
    )
    db.add(conversation)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="conversation.created",
        summary="En workspace-konversation startades",
        detail={"conversation_id": str(conversation.id)},
    )
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/{project_id}/activity", response_model=list[WorkspaceActivityOut])
def list_workspace_activity(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        WorkspaceActivityOut.model_validate(activity)
        for activity in _workspace_activities(db, project, user, limit=100)
    ]


@router.get("/{project_id}/decisions", response_model=list[WorkspaceDecisionOut])
def list_workspace_decisions(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        WorkspaceDecisionOut.model_validate(decision)
        for decision in _workspace_decisions(db, project, user, limit=100)
    ]


@router.post("/{project_id}/decisions", response_model=WorkspaceDecisionOut)
def create_workspace_decision(
    project_id: uuid.UUID,
    payload: WorkspaceDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    decision = WorkspaceDecision(
        project_id=project.id,
        owner_id=user.id,
        title=payload.title,
        decision=payload.decision,
        rationale=payload.rationale,
        status=payload.status,
    )
    db.add(decision)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="decision.recorded",
        summary=f"Beslut registrerades: {decision.title}",
        detail={"decision_id": str(decision.id)},
    )
    db.commit()
    db.refresh(decision)
    return decision


@router.get("/{project_id}/context", response_model=WorkspaceContextOut)
async def get_workspace_context(
    project_id: uuid.UUID,
    q: str | None = Query(None, max_length=2000),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return await _build_context(db, project=project, user=user, query=q)
