"""MainAI Project Memory & Coordination Loop — API surface (see app/project_memory.py for
the actual storage/retrieval logic this only exposes over HTTP). Founder-only, like every
other admin route — this is project-wide state, not per-user data."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.models.project_memory import NoteKind, NoteStatus, SideIssueClassification
from app.models.user import User
from app.project_memory import (
    add_note,
    classify_note,
    create_checkpoint,
    detect_conflicts_and_duplicates,
    get_latest_checkpoint,
    ingest_doc,
    ingest_git_commit,
    ingest_github_status,
    is_checkpoint_stale,
    list_checkpoints,
    list_current_branch_pr_status,
    list_notes,
    list_notes_needing_founder_decision,
    list_sources,
    read_checkpoint_brief,
    read_source_content,
    resolve_note,
)
from app.schemas import (
    ProjectBranchPRStatusOut,
    ProjectCheckpointDetailOut,
    ProjectCheckpointIn,
    ProjectCheckpointOut,
    ProjectCheckpointStaleOut,
    ProjectConflictsOut,
    ProjectGithubStatusIn,
    ProjectNoteClassifyIn,
    ProjectNoteIn,
    ProjectNoteOut,
    ProjectNoteResolveIn,
    ProjectSourceDetailOut,
    ProjectSourceIngestDocIn,
    ProjectSourceOut,
)

router = APIRouter(prefix="/api/admin/memory", tags=["memory"], dependencies=[Depends(require_founder)])


# --- Notes ------------------------------------------------------------------------------


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
        source_id=payload.source_id,
        classification=SideIssueClassification(payload.classification) if payload.classification else None,
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


@router.get("/notes/needs-founder-decision", response_model=list[ProjectNoteOut])
def get_notes_needing_founder_decision(db: Session = Depends(get_db)):
    """Fas 2 Founder Decision Gate: only what's already explicitly classified this way —
    never computed by inference."""
    return list_notes_needing_founder_decision(db)


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


@router.post("/notes/{note_id}/classify", response_model=ProjectNoteOut)
def classify_note_route(
    note_id: uuid.UUID,
    payload: ProjectNoteClassifyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        note = classify_note(db, note_id, classification=SideIssueClassification(payload.classification))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(
        db,
        user_id=user.id,
        action="project_note_classified",
        entity_type="project_note",
        entity_id=str(note.id),
        detail=payload.classification,
        request=request,
    )
    return note


# --- Source ingestion ---------------------------------------------------------------------


@router.post("/sources/doc", response_model=ProjectSourceOut)
def ingest_doc_route(
    payload: ProjectSourceIngestDocIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        source = ingest_doc(db, relative_path=payload.relative_path, ingested_by=user.email)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        user_id=user.id,
        action="project_source_doc_ingested",
        entity_type="project_source",
        entity_id=str(source.id),
        detail=payload.relative_path,
        request=request,
    )
    return source


@router.post("/sources/git-commit", response_model=ProjectSourceOut)
def ingest_git_commit_route(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        source = ingest_git_commit(db, ingested_by=user.email)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        user_id=user.id,
        action="project_source_git_commit_ingested",
        entity_type="project_source",
        entity_id=str(source.id),
        detail=source.commit_sha,
        request=request,
    )
    return source


@router.get("/sources", response_model=list[ProjectSourceOut])
def get_sources(source_type: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    return list_sources(db, source_type=source_type, limit=limit)


@router.get("/sources/{source_id}", response_model=ProjectSourceDetailOut)
def get_source_detail(source_id: uuid.UUID, db: Session = Depends(get_db)):
    from app.models.project_memory import ProjectSource

    source = db.get(ProjectSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Ingen sådan källa.")
    content = read_source_content(source) if source.storage_key else ""
    return ProjectSourceDetailOut(**ProjectSourceOut.model_validate(source).model_dump(), content=content)


# --- Branch/PR status (agent-supplied, see app/project_memory.ingest_github_status) --------


@router.post("/branch-pr-status", response_model=ProjectBranchPRStatusOut)
def record_branch_pr_status(
    payload: ProjectGithubStatusIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    entry = ingest_github_status(
        db,
        kind=payload.kind,
        ref=payload.ref,
        status=payload.status,
        title=payload.title,
        base_ref=payload.base_ref,
        head_ref=payload.head_ref,
        mergeable=payload.mergeable,
        ci_status=payload.ci_status,
        summary=payload.summary,
        ingested_by=user.email,
    )
    record_audit(
        db,
        user_id=user.id,
        action="project_branch_pr_status_recorded",
        entity_type="project_branch_pr_status",
        entity_id=str(entry.id),
        detail=f"{payload.kind} {payload.ref}: {payload.status}",
        request=request,
    )
    return entry


@router.get("/branch-pr-status", response_model=list[ProjectBranchPRStatusOut])
def get_branch_pr_status(kind: str | None = None, db: Session = Depends(get_db)):
    return list_current_branch_pr_status(db, kind=kind)


# --- Conflict / duplicate-work detection ----------------------------------------------------


@router.get("/conflicts", response_model=ProjectConflictsOut)
def get_conflicts(db: Session = Depends(get_db)):
    """Flags possible overlap/contradictions for human review — never resolves or decides
    anything (Fas 2 requirement 6)."""
    return detect_conflicts_and_duplicates(db)


# --- Checkpoints -----------------------------------------------------------------------------


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


@router.get("/checkpoints/{checkpoint_id}/stale", response_model=ProjectCheckpointStaleOut)
def get_checkpoint_stale(checkpoint_id: uuid.UUID, db: Session = Depends(get_db)):
    from app.models.project_memory import ProjectCheckpoint

    checkpoint = db.get(ProjectCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Ingen sådan checkpoint.")
    stale, reasons = is_checkpoint_stale(db, checkpoint)
    return ProjectCheckpointStaleOut(stale=stale, reasons=reasons)
