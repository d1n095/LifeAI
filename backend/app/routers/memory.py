"""MainAI Project Memory & Coordination Loop — API surface (see app/project_memory.py for
the actual storage/retrieval logic this only exposes over HTTP). Founder-only, like every
other admin route — this is project-wide state, not per-user data."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.models.project_memory import NoteKind, NoteStatus
from app.models.user import User
from app.project_memory import add_note, create_checkpoint, get_latest_checkpoint, list_checkpoints, list_notes, read_checkpoint_brief, resolve_note
from app.schemas import (
    ProjectCheckpointDetailOut,
    ProjectCheckpointIn,
    ProjectCheckpointOut,
    ProjectNoteIn,
    ProjectNoteOut,
    ProjectNoteResolveIn,
)

router = APIRouter(prefix="/api/admin/memory", tags=["memory"], dependencies=[Depends(require_founder)])


@router.post("/notes", response_model=ProjectNoteOut)
def create_note(
    payload: ProjectNoteIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    note = add_note(
        db,
        kind=NoteKind(payload.kind),
        content=payload.content,
        source_type=payload.source_type,
        source_ref=payload.source_ref,
        created_by=user.email,
    )
    record_audit(
        db,
        user_id=user.id,
        action="project_note_created",
        entity_type="project_note",
        entity_id=str(note.id),
        detail=f"{note.kind.value}: {note.source_type} {note.source_ref}",
        request=request,
    )
    return note


@router.get("/notes", response_model=list[ProjectNoteOut])
def get_notes(status: str | None = "open", db: Session = Depends(get_db)):
    """`status=open` (default) answers "what's true right now". `status=all` returns full
    history including resolved/superseded notes — the explicit current-vs-history
    distinction CLAUDE.md's success metric requires."""
    if status == "all":
        note_status = None
    elif status in ("open", "resolved", "superseded"):
        note_status = NoteStatus(status)
    else:
        raise HTTPException(status_code=400, detail="status måste vara 'open', 'resolved', 'superseded' eller 'all'.")
    return list_notes(db, status=note_status)


@router.post("/notes/{note_id}/resolve", response_model=ProjectNoteOut)
def resolve_note_route(
    note_id: uuid.UUID,
    payload: ProjectNoteResolveIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        note = resolve_note(
            db,
            note_id,
            resolved_by=user.email,
            resolution_note=payload.resolution_note,
            superseded=payload.superseded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(
        db,
        user_id=user.id,
        action="project_note_resolved" if not payload.superseded else "project_note_superseded",
        entity_type="project_note",
        entity_id=str(note.id),
        detail=payload.resolution_note,
        request=request,
    )
    return note


@router.post("/checkpoints", response_model=ProjectCheckpointDetailOut)
def create_checkpoint_route(
    payload: ProjectCheckpointIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    checkpoint = create_checkpoint(
        db,
        summary=payload.summary,
        branch_name=payload.branch_name,
        open_pr_refs=payload.open_pr_refs,
        created_by=user.email,
    )
    record_audit(
        db,
        user_id=user.id,
        action="project_checkpoint_created",
        entity_type="project_checkpoint",
        entity_id=str(checkpoint.id),
        detail=payload.branch_name,
        request=request,
    )
    return ProjectCheckpointDetailOut(
        **ProjectCheckpointOut.model_validate(checkpoint).model_dump(),
        brief=read_checkpoint_brief(checkpoint),
    )


@router.get("/checkpoints", response_model=list[ProjectCheckpointOut])
def get_checkpoints(limit: int = 20, db: Session = Depends(get_db)):
    return list_checkpoints(db, limit=limit)


@router.get("/checkpoints/latest", response_model=ProjectCheckpointDetailOut)
def get_latest_checkpoint_route(db: Session = Depends(get_db)):
    """The single most important read in this whole loop: what a new Claude session — or the
    founder — should fetch first to resume work without guessing."""
    checkpoint = get_latest_checkpoint(db)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Ingen checkpoint finns ännu.")
    return ProjectCheckpointDetailOut(
        **ProjectCheckpointOut.model_validate(checkpoint).model_dump(),
        brief=read_checkpoint_brief(checkpoint),
    )
