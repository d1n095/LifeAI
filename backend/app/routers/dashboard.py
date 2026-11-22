"""Founder dashboard aggregation.

One bounded read path over the existing project, task, calendar, document, conversation and
MainAI execution tables. It performs a fixed number of aggregate/list queries instead of
letting the browser fetch every collection and count client-side.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.calendar import CalendarEvent
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.mainai_execution import (
    ACTIVE_MAINAI_GOAL_STATUSES,
    MainAIGoal,
    MainAITask,
    MainAITaskStatus,
)
from app.models.project import Project, Task, TaskStatus
from app.models.user import User
from app.models.work_management import TaskDependency
from app.models.workspace import WorkspaceDecision
from app.schemas import (
    CalendarEventOut,
    ConversationOut,
    DashboardCountsOut,
    DashboardOut,
    KnowledgeSourceOut,
    MainAIGoalOut,
    MainAITaskOut,
    TaskOut,
    WorkspaceDecisionOut,
    WorkspaceOut,
)
from app.work_management import task_outs

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(require_founder)])


@router.get("", response_model=DashboardOut)
def get_dashboard(db: Session = Depends(get_db), user: User = Depends(require_founder)) -> DashboardOut:
    now = datetime.utcnow()
    horizon = now + timedelta(days=14)

    project_query = db.query(Project).filter(
        Project.created_by == user.id,
        Project.deleted_at.is_(None),
        Project.archived_at.is_(None),
    )
    active_projects = (
        project_query.filter(Project.status == "active")
        .order_by(Project.updated_at.desc(), Project.id.asc())
        .limit(6)
        .all()
    )
    active_project_count = (
        db.query(func.count(Project.id))
        .filter(
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
            Project.archived_at.is_(None),
            Project.status == "active",
        )
        .scalar()
        or 0
    )

    open_statuses = [TaskStatus.todo, TaskStatus.in_progress, TaskStatus.blocked]
    task_query = db.query(Task).filter(
        Task.created_by == user.id,
        Task.deleted_at.is_(None),
        Task.status.in_(open_statuses),
    )
    open_task_count = task_query.count()
    outstanding = (
        task_query.order_by(Task.due_at.asc().nulls_last(), Task.created_at.asc())
        .limit(6)
        .all()
    )
    blocked_status_count = (
        db.query(func.count(Task.id))
        .filter(
            Task.created_by == user.id,
            Task.deleted_at.is_(None),
            Task.status == TaskStatus.blocked,
        )
        .scalar()
        or 0
    )
    dep = Task.__table__.alias("dep")
    dependency_blocked_count = (
        db.query(func.count(func.distinct(TaskDependency.task_id)))
        .join(Task, Task.id == TaskDependency.task_id)
        .join(dep, TaskDependency.depends_on_task_id == dep.c.id)
        .filter(
            TaskDependency.owner_id == user.id,
            Task.created_by == user.id,
            Task.deleted_at.is_(None),
            Task.status.in_(open_statuses),
            dep.c.status.notin_([TaskStatus.done, TaskStatus.cancelled]),
        )
        .scalar()
        or 0
    )
    blocked_tasks = (
        task_query.filter(Task.status == TaskStatus.blocked)
        .order_by(Task.updated_at.desc(), Task.id.asc())
        .limit(6)
        .all()
    )

    upcoming_events = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.owner_id == user.id,
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status == "scheduled",
            CalendarEvent.starts_at >= now,
            CalendarEvent.starts_at < horizon,
        )
        .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
        .limit(6)
        .all()
    )
    upcoming_event_count = (
        db.query(func.count(CalendarEvent.id))
        .filter(
            CalendarEvent.owner_id == user.id,
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status == "scheduled",
            CalendarEvent.starts_at >= now,
            CalendarEvent.starts_at < horizon,
        )
        .scalar()
        or 0
    )

    recent_decisions = (
        db.query(WorkspaceDecision)
        .join(Project, Project.id == WorkspaceDecision.project_id)
        .filter(
            WorkspaceDecision.owner_id == user.id,
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
        )
        .order_by(WorkspaceDecision.created_at.desc(), WorkspaceDecision.id.asc())
        .limit(6)
        .all()
    )
    recent_decision_count = (
        db.query(func.count(WorkspaceDecision.id))
        .join(Project, Project.id == WorkspaceDecision.project_id)
        .filter(
            WorkspaceDecision.owner_id == user.id,
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
        )
        .scalar()
        or 0
    )

    recent_documents = (
        db.query(Document)
        .filter(Document.uploaded_by == user.id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc(), Document.id.asc())
        .limit(6)
        .all()
    )
    recent_document_count = (
        db.query(func.count(Document.id))
        .filter(Document.uploaded_by == user.id, Document.deleted_at.is_(None))
        .scalar()
        or 0
    )

    active_conversations = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.asc())
        .limit(6)
        .all()
    )
    active_conversation_count = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.user_id == user.id)
        .scalar()
        or 0
    )

    supervised_goals = (
        db.query(MainAIGoal)
        .filter(
            MainAIGoal.owner_id == user.id,
            MainAIGoal.status.in_(list(ACTIVE_MAINAI_GOAL_STATUSES)),
        )
        .order_by(MainAIGoal.created_at.desc(), MainAIGoal.id.asc())
        .limit(6)
        .all()
    )
    active_goal_count = (
        db.query(func.count(MainAIGoal.id))
        .filter(
            MainAIGoal.owner_id == user.id,
            MainAIGoal.status.in_(list(ACTIVE_MAINAI_GOAL_STATUSES)),
        )
        .scalar()
        or 0
    )
    supervised_tasks = (
        db.query(MainAITask)
        .filter(
            MainAITask.owner_id == user.id,
            MainAITask.status.in_(
                [
                    MainAITaskStatus.running,
                    MainAITaskStatus.ready,
                    MainAITaskStatus.blocked,
                    MainAITaskStatus.waiting_external,
                    MainAITaskStatus.waiting_ci,
                ]
            ),
        )
        .order_by(MainAITask.created_at.desc(), MainAITask.id.asc())
        .limit(6)
        .all()
    )
    running_task_count = (
        db.query(func.count(MainAITask.id))
        .filter(MainAITask.owner_id == user.id, MainAITask.status == MainAITaskStatus.running)
        .scalar()
        or 0
    )
    awaiting_approval_count = (
        db.query(func.count(MainAITask.id))
        .filter(
            MainAITask.owner_id == user.id,
            MainAITask.approval_required.is_(True),
            MainAITask.status.in_(
                [MainAITaskStatus.pending, MainAITaskStatus.ready, MainAITaskStatus.blocked]
            ),
        )
        .scalar()
        or 0
    )

    return DashboardOut(
        generated_at=now,
        counts=DashboardCountsOut(
            active_projects=active_project_count,
            open_tasks=open_task_count,
            blocked_tasks=max(blocked_status_count, dependency_blocked_count),
            upcoming_events=upcoming_event_count,
            recent_decisions=recent_decision_count,
            recent_documents=recent_document_count,
            active_conversations=active_conversation_count,
            active_mainai_goals=active_goal_count,
            running_mainai_tasks=running_task_count,
            awaiting_approval_tasks=awaiting_approval_count,
        ),
        active_projects=[WorkspaceOut.model_validate(item) for item in active_projects],
        outstanding_tasks=task_outs(db, outstanding, user.id),
        blocked_tasks=task_outs(db, blocked_tasks, user.id),
        upcoming_events=[CalendarEventOut.model_validate(item) for item in upcoming_events],
        recent_decisions=[WorkspaceDecisionOut.model_validate(item) for item in recent_decisions],
        recent_documents=[KnowledgeSourceOut.model_validate(item) for item in recent_documents],
        active_conversations=[ConversationOut.model_validate(item) for item in active_conversations],
        supervised_goals=[MainAIGoalOut.model_validate(item) for item in supervised_goals],
        supervised_tasks=[MainAITaskOut.model_validate(item) for item in supervised_tasks],
    )
