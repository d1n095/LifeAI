"""Authorized search across the founder's personal work objects.

Every query is owner-filtered before any title/description is read or returned. Results are
assembled from the existing tables (projects, tasks, documents, conversations, calendar
events); no parallel search index or cross-user corpus is introduced.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.calendar import CalendarEvent
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.project import Project, Task
from app.models.user import User
from app.schemas import SearchResponseOut, SearchResultOut

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[Depends(require_founder)])

_SEARCH_TYPES = ("project", "task", "document", "conversation", "calendar_event")


@router.get("", response_model=SearchResponseOut)
def search_personal_work(
    q: str = Query(min_length=2, max_length=200),
    type: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> SearchResponseOut:
    requested = {item.strip() for item in (type or "").split(",") if item.strip()}
    if requested and not requested.issubset(set(_SEARCH_TYPES)):
        requested = set()
    selected = requested or set(_SEARCH_TYPES)
    needle = f"%{q.strip()}%"
    project_filter = None
    if project_id:
        try:
            import uuid

            project_filter = uuid.UUID(project_id)
        except ValueError:
            project_filter = None

    results: list[SearchResultOut] = []
    total = 0
    fetch = min(offset + limit, 200)

    if "project" in selected:
        query = db.query(Project).filter(
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
            (Project.name.ilike(needle) | Project.description.ilike(needle) | Project.purpose.ilike(needle)),
        )
        if date_from:
            query = query.filter(Project.updated_at >= date_from)
        if date_to:
            query = query.filter(Project.updated_at <= date_to)
        total += query.count()
        for row in query.order_by(Project.updated_at.desc(), Project.id.asc()).limit(fetch).all():
            results.append(
                SearchResultOut(
                    type="project",
                    id=row.id,
                    title=row.name,
                    snippet=(row.purpose or row.description or "Projekt")[:240],
                    project_id=row.id,
                    updated_at=row.updated_at,
                    href=f"/projects/{row.id}",
                )
            )

    if "task" in selected:
        query = db.query(Task).filter(
            Task.created_by == user.id,
            Task.deleted_at.is_(None),
            (Task.title.ilike(needle) | Task.description.ilike(needle)),
        )
        if project_filter:
            query = query.filter(Task.project_id == project_filter)
        if date_from:
            query = query.filter(Task.updated_at >= date_from)
        if date_to:
            query = query.filter(Task.updated_at <= date_to)
        total += query.count()
        for row in query.order_by(Task.updated_at.desc(), Task.id.asc()).limit(fetch).all():
            results.append(
                SearchResultOut(
                    type="task",
                    id=row.id,
                    title=row.title,
                    snippet=(row.description or f"Status: {row.status}")[:240],
                    project_id=row.project_id,
                    updated_at=row.updated_at,
                    href=f"/tasks?task={row.id}",
                )
            )

    if "document" in selected:
        query = db.query(Document).filter(
            Document.uploaded_by == user.id,
            Document.deleted_at.is_(None),
            Document.title.ilike(needle),
        )
        if project_filter:
            query = query.filter(Document.project_id == project_filter)
        if date_from:
            query = query.filter(Document.created_at >= date_from)
        if date_to:
            query = query.filter(Document.created_at <= date_to)
        total += query.count()
        for row in query.order_by(Document.created_at.desc(), Document.id.asc()).limit(fetch).all():
            results.append(
                SearchResultOut(
                    type="document",
                    id=row.id,
                    title=row.title,
                    snippet=f"Kunskapskälla · {row.status}",
                    project_id=row.project_id,
                    occurred_at=row.created_at,
                    updated_at=row.updated_at,
                    href=f"/library/{row.id}",
                )
            )

    if "conversation" in selected:
        query = db.query(Conversation).filter(
            Conversation.user_id == user.id,
            Conversation.title.ilike(needle),
        )
        if project_filter:
            query = query.filter(Conversation.project_id == project_filter)
        if date_from:
            query = query.filter(Conversation.updated_at >= date_from)
        if date_to:
            query = query.filter(Conversation.updated_at <= date_to)
        total += query.count()
        for row in query.order_by(Conversation.updated_at.desc(), Conversation.id.asc()).limit(fetch).all():
            results.append(
                SearchResultOut(
                    type="conversation",
                    id=row.id,
                    title=row.title,
                    snippet="Konversation med MainAI",
                    project_id=row.project_id,
                    updated_at=row.updated_at,
                    href=f"/chat?conversation={row.id}",
                )
            )

    if "calendar_event" in selected:
        query = db.query(CalendarEvent).filter(
            CalendarEvent.owner_id == user.id,
            CalendarEvent.deleted_at.is_(None),
            (
                CalendarEvent.title.ilike(needle)
                | CalendarEvent.description.ilike(needle)
                | CalendarEvent.location.ilike(needle)
            ),
        )
        if project_filter:
            query = query.filter(CalendarEvent.project_id == project_filter)
        if date_from:
            query = query.filter(CalendarEvent.starts_at >= date_from)
        if date_to:
            query = query.filter(CalendarEvent.starts_at <= date_to)
        total += query.count()
        for row in query.order_by(CalendarEvent.starts_at.desc(), CalendarEvent.id.asc()).limit(fetch).all():
            results.append(
                SearchResultOut(
                    type="calendar_event",
                    id=row.id,
                    title=row.title,
                    snippet=f"{row.category} · {row.starts_at.isoformat()}",
                    project_id=row.project_id,
                    occurred_at=row.starts_at,
                    updated_at=row.updated_at,
                    href=f"/calendar?event={row.id}",
                )
            )

    results.sort(
        key=lambda item: (item.updated_at or item.occurred_at or datetime.min, str(item.id)),
        reverse=True,
    )
    return SearchResponseOut(
        query=q,
        results=results[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
        types=sorted(selected),
    )
