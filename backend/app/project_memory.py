"""MainAI Project Memory & Coordination Loop — first working slice.

Deliberately narrow: this module only stores/retrieves structured project facts (decisions,
blockers, next steps, checkpoints) with source citations, and can render them into a
resumption brief. It does not read git/GitHub state itself, does not decide anything, and
does not merge/deploy/change governance — see CLAUDE.md's "Malet" section for why: this is
the storage+retrieval substrate a human (or, later, MainAI itself) writes structured facts
into, not an autonomous agent.

Reuses existing infrastructure rather than duplicating it (per the 2026-07-26 direction):
  - app.storage.get_storage() — the same content-addressed blob store documents use — holds
    the full resumption-brief markdown; the DB only indexes it (see ProjectCheckpoint).
  - The same "never delete, mark resolved/superseded instead" pattern KnowledgeVersion/
    ImportJob already use for history vs. current state (see NoteStatus).
"""

import io
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project_memory import NoteKind, NoteStatus, ProjectCheckpoint, ProjectCheckpointNote, ProjectNote
from app.storage import get_storage

MAX_BRIEF_BYTES = 2 * 1024 * 1024  # a resumption brief is text; 2MB is already generous


def add_note(
    db: Session,
    *,
    kind: NoteKind,
    content: str,
    source_type: str,
    source_ref: str,
    created_by: str,
) -> ProjectNote:
    """Records one decision/blocker/next-step. `source_type`/`source_ref` are required, not
    optional — a note with no citation is exactly the "guessing instead of pointing at
    evidence" failure mode this table exists to prevent."""
    note = ProjectNote(
        kind=kind,
        content=content,
        source_type=source_type,
        source_ref=source_ref,
        created_by=created_by,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def resolve_note(
    db: Session,
    note_id: uuid.UUID,
    *,
    resolved_by: str,
    resolution_note: str,
    superseded: bool = False,
) -> ProjectNote:
    """Marks a note resolved (done/no longer applies) or superseded (replaced by a newer
    note) — never deletes it. A superseded note should have a `resolution_note` that points
    at whatever replaced it (e.g. "see note <uuid>"), following the same citation
    requirement as creation."""
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise ValueError(f"Ingen ProjectNote med id={note_id}")
    note.status = NoteStatus.superseded if superseded else NoteStatus.resolved
    note.resolved_at = datetime.utcnow()
    note.resolved_by = resolved_by
    note.resolution_note = resolution_note
    db.commit()
    db.refresh(note)
    return note


def list_notes(db: Session, *, status: NoteStatus | None = NoteStatus.open, kind: NoteKind | None = None) -> list[ProjectNote]:
    """Default (`status=NoteStatus.open`) answers "what's true right now" — pass
    `status=None` to get full history including resolved/superseded rows."""
    stmt = select(ProjectNote).order_by(ProjectNote.created_at.desc())
    if status is not None:
        stmt = stmt.where(ProjectNote.status == status)
    if kind is not None:
        stmt = stmt.where(ProjectNote.kind == kind)
    return list(db.execute(stmt).scalars())


def generate_resumption_brief(
    *,
    summary: str,
    branch_name: str,
    open_pr_refs: list[str],
    open_notes: list[ProjectNote],
) -> str:
    """Pure function: builds the exact markdown a new Claude session should be able to read
    and correctly answer, without guessing, what the project builds, what's done, what's
    blocked, which branch/PR applies, and what the next safe step is (see CLAUDE.md's success
    metric). Every decision/blocker/next-step line carries its source citation inline."""
    lines = [
        "# MainAI Project Checkpoint",
        "",
        f"**Genererad:** {datetime.utcnow().isoformat()}Z",
        f"**Aktuell branch:** `{branch_name}`",
        f"**Öppna PR:er:** {', '.join(open_pr_refs) if open_pr_refs else '(inga)'}",
        "",
        "## Sammanfattning",
        "",
        summary,
        "",
    ]

    for kind, heading in (
        (NoteKind.blocker, "## Blockerare (öppna)"),
        (NoteKind.decision, "## Beslut (gällande)"),
        (NoteKind.next_step, "## Nästa steg"),
    ):
        matching = [n for n in open_notes if n.kind == kind]
        lines.append(heading)
        lines.append("")
        if not matching:
            lines.append("(inga)")
        else:
            for note in matching:
                lines.append(f"- {note.content} — källa: {note.source_type} {note.source_ref}")
        lines.append("")

    lines.append(
        "_Detta är en genererad återupptagningsbrief — varje rad ovan är spårbar till en "
        "ProjectNote-rad med källhänvisning, inte fritext utan ursprung._"
    )
    return "\n".join(lines)


def create_checkpoint(
    db: Session,
    *,
    summary: str,
    branch_name: str,
    open_pr_refs: list[str],
    created_by: str,
) -> ProjectCheckpoint:
    """Snapshots current state: reads all currently-open notes, renders the resumption
    brief, stores it durably via the shared content-addressed storage backend, and records a
    ProjectCheckpoint row plus one ProjectCheckpointNote link per open note included — so the
    brief's claims are checkable against real rows later, not just trusted as free text."""
    open_notes = list_notes(db, status=NoteStatus.open)
    brief = generate_resumption_brief(
        summary=summary,
        branch_name=branch_name,
        open_pr_refs=open_pr_refs,
        open_notes=open_notes,
    )
    brief_bytes = brief.encode("utf-8")

    storage = get_storage()
    reader = io.BytesIO(brief_bytes)
    blob = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=MAX_BRIEF_BYTES)

    checkpoint = ProjectCheckpoint(
        summary=summary,
        branch_name=branch_name,
        open_pr_refs=",".join(open_pr_refs),
        brief_storage_key=blob.storage_key,
        brief_sha256=blob.sha256,
        created_by=created_by,
    )
    db.add(checkpoint)
    db.flush()  # assigns checkpoint.id before the FK rows below

    for note in open_notes:
        db.add(ProjectCheckpointNote(checkpoint_id=checkpoint.id, note_id=note.id))

    db.commit()
    db.refresh(checkpoint)
    return checkpoint


def get_latest_checkpoint(db: Session) -> ProjectCheckpoint | None:
    stmt = select(ProjectCheckpoint).order_by(ProjectCheckpoint.created_at.desc()).limit(1)
    return db.execute(stmt).scalars().first()


def list_checkpoints(db: Session, *, limit: int = 20) -> list[ProjectCheckpoint]:
    stmt = select(ProjectCheckpoint).order_by(ProjectCheckpoint.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())


def read_checkpoint_brief(checkpoint: ProjectCheckpoint) -> str:
    """Reads the full resumption-brief markdown back from durable storage — the actual text
    a new session should read to resume, not just the summary/metadata on the row."""
    storage = get_storage()
    with storage.open_read(checkpoint.brief_storage_key) as f:
        return f.read().decode("utf-8")
