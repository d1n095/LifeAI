"""MainAI Project Memory & Coordination Loop.

Deliberately narrow: this module stores/retrieves structured project facts (decisions,
blockers, next steps, status facts, uncertainties, ingested sources, branch/PR snapshots,
checkpoints) with source citations, detects possible overlap/duplicate work, and renders a
resumption brief. It does not merge/deploy/force-push/delete branches/change governance, and
it does not compute side-issue classifications or conflict resolutions autonomously — those
are always supplied or reviewed by the caller (the agent or the founder). See CLAUDE.md's
"Malet" section for the exact boundary.

Reuses existing infrastructure rather than duplicating it:
  - app.storage.get_storage() — the same content-addressed blob store documents use — holds
    the full resumption-brief markdown and ingested doc content; the DB only indexes it.
  - The same "never delete, mark resolved/superseded instead" pattern KnowledgeVersion/
    ImportJob already use for history vs. current state.

GitHub state is ingested as structured input the caller already fetched (via its own
credentials/tooling, e.g. an agent's GitHub MCP tools) — there is no in-process GitHub client
anywhere in this codebase, so this module never re-fetches it itself (see
ingest_github_status()).
"""

import io
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.project_memory import (
    NoteKind,
    NoteStatus,
    ProjectBranchPRStatus,
    ProjectCheckpoint,
    ProjectCheckpointNote,
    ProjectNote,
    ProjectSource,
    SideIssueClassification,
)
from app.storage import get_storage

MAX_BRIEF_BYTES = 2 * 1024 * 1024  # a resumption brief is text; 2MB is already generous
MAX_DOC_BYTES = 10 * 1024 * 1024  # a governing doc is text; 10MB is already generous

PROHIBITIONS = [
    "Merga en pull request.",
    "Deploya eller på annat sätt driftsätta kod.",
    "Force-pusha eller skriva om historik på en delad branch.",
    "Radera en branch.",
    "Ändra P7A eller annan governance-plan utan ett separat, uttryckligt beslut.",
    "Fatta produkt-, kostnads-, säkerhets- eller arkitekturbeslut på egen hand — fråga grundaren.",
]


def _require_project_root() -> Path:
    root = get_settings().project_root
    if not root:
        raise ValueError(
            "PROJECT_ROOT är inte satt — doc-/git-ingestion kräver att den fullständiga "
            "repo-checkouten finns tillgänglig (endast sant i en dev-/CI-/agentsession, se "
            "app/config.py:s kommentar om varför produktionsbackenden aldrig har den)."
        )
    path = Path(root)
    if not path.is_dir():
        raise ValueError(f"PROJECT_ROOT ({root!r}) pekar inte på en befintlig katalog.")
    return path


def _run_git(project_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} misslyckades: {result.stderr.strip()}")
    return result.stdout.strip()


# --- Notes: decisions, blockers, next steps, facts, uncertainties -------------------------


def add_note(
    db: Session,
    *,
    kind: NoteKind,
    content: str,
    source_type: str,
    source_ref: str,
    created_by: str,
    source_id: uuid.UUID | None = None,
    classification: SideIssueClassification | None = None,
) -> ProjectNote:
    """Records one project-memory fact. `source_type`/`source_ref` are required, not
    optional — a note with no citation is exactly the "guessing instead of pointing at
    evidence" failure mode this table exists to prevent. `classification` is only meaningful
    for side-issue findings (Fas 2 requirement 7) — the caller decides the bucket, this
    function only stores it."""
    note = ProjectNote(
        kind=kind,
        content=content,
        source_type=source_type,
        source_ref=source_ref,
        source_id=source_id,
        classification=classification,
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
    note) — never deletes it."""
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


def list_notes_needing_founder_decision(db: Session) -> list[ProjectNote]:
    """Fas 2 requirement 8 (Founder Decision Gate): the queryable list of open findings
    explicitly classified as requiring the founder — never computed by inference, only ever
    what a caller already classified that way via add_note()/classify_note()."""
    stmt = (
        select(ProjectNote)
        .where(ProjectNote.status == NoteStatus.open)
        .where(ProjectNote.classification == SideIssueClassification.needs_founder_decision)
        .order_by(ProjectNote.created_at.desc())
    )
    return list(db.execute(stmt).scalars())


def classify_note(db: Session, note_id: uuid.UUID, *, classification: SideIssueClassification) -> ProjectNote:
    """Sets/updates a note's side-issue classification after the fact (e.g. a `fact`/
    `uncertainty` note turns out to need founder input once more is known)."""
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise ValueError(f"Ingen ProjectNote med id={note_id}")
    note.classification = classification
    db.commit()
    db.refresh(note)
    return note


# --- Source ingestion -----------------------------------------------------------------------


def ingest_doc(db: Session, *, relative_path: str, ingested_by: str) -> ProjectSource:
    """Reads one governing doc (e.g. "CLAUDE.md", "docs/BRANCH_REGISTRY.md") from
    PROJECT_ROOT, stores its content durably via the shared content-addressed storage
    backend, and records a ProjectSource row citing it. Raises if PROJECT_ROOT is unset or
    the file doesn't exist — never silently skips (a missing governing doc is itself a fact
    worth failing loudly on, not guessing past)."""
    project_root = _require_project_root()
    doc_path = (project_root / relative_path).resolve()
    try:
        doc_path.relative_to(project_root.resolve())
    except ValueError:
        raise ValueError(f"{relative_path!r} pekar utanför PROJECT_ROOT.") from None
    if not doc_path.is_file():
        raise FileNotFoundError(f"Ingen sådan fil: {relative_path} (under PROJECT_ROOT={project_root})")

    content = doc_path.read_bytes()
    if len(content) > MAX_DOC_BYTES:
        raise ValueError(f"{relative_path} är större än {MAX_DOC_BYTES} bytes — avbryter ingestion.")

    storage = get_storage()
    reader = io.BytesIO(content)
    blob = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=MAX_DOC_BYTES)

    commit_sha: str | None = None
    try:
        commit_sha = _run_git(project_root, "rev-parse", "HEAD")
    except Exception:  # noqa: BLE001 - git may be unavailable; doc ingestion must still succeed
        commit_sha = None

    source = ProjectSource(
        source_type="doc",
        source_ref=relative_path,
        content_sha256=blob.sha256,
        storage_key=blob.storage_key,
        commit_sha=commit_sha,
        ingested_by=ingested_by,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def read_source_content(source: ProjectSource) -> str:
    """Reads an ingested doc's full text back from durable storage."""
    if source.storage_key is None:
        raise ValueError(f"ProjectSource {source.id} har ingen lagrad text (storage_key saknas).")
    storage = get_storage()
    with storage.open_read(source.storage_key) as f:
        return f.read().decode("utf-8")


def ingest_git_commit(db: Session, *, ingested_by: str) -> ProjectSource:
    """Snapshots the local repo's current HEAD (commit SHA, branch name, subject line) under
    PROJECT_ROOT. This is the basis for checkpoint staleness detection (see
    is_checkpoint_stale())."""
    project_root = _require_project_root()
    commit_sha = _run_git(project_root, "rev-parse", "HEAD")
    branch = _run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    subject = _run_git(project_root, "log", "-1", "--format=%s")

    source = ProjectSource(
        source_type="git_commit",
        source_ref="HEAD",
        commit_sha=commit_sha,
        raw_data={"commit_sha": commit_sha, "branch": branch, "subject": subject},
        ingested_by=ingested_by,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def ingest_github_status(
    db: Session,
    *,
    kind: str,
    ref: str,
    status: str,
    ingested_by: str,
    title: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    mergeable: bool | None = None,
    ci_status: str | None = None,
    summary: str | None = None,
) -> ProjectBranchPRStatus:
    """Records a branch/PR status snapshot the CALLER already fetched (e.g. via its own
    GitHub MCP tools) — this function never talks to GitHub itself, since no in-process
    GitHub client exists in this codebase (see module docstring). Supersedes any prior
    current row for the same (kind, ref) rather than overwriting it — history is preserved."""
    if kind not in ("branch", "pr"):
        raise ValueError("kind måste vara 'branch' eller 'pr'.")

    source = ProjectSource(
        source_type=f"github_{kind}",
        source_ref=ref,
        raw_data={
            "kind": kind,
            "ref": ref,
            "title": title,
            "status": status,
            "base_ref": base_ref,
            "head_ref": head_ref,
            "mergeable": mergeable,
            "ci_status": ci_status,
        },
        ingested_by=ingested_by,
    )
    db.add(source)
    db.flush()

    now = datetime.utcnow()
    prior_stmt = (
        select(ProjectBranchPRStatus)
        .where(ProjectBranchPRStatus.kind == kind)
        .where(ProjectBranchPRStatus.ref == ref)
        .where(ProjectBranchPRStatus.is_current.is_(True))
    )
    for prior in db.execute(prior_stmt).scalars():
        prior.is_current = False
        prior.superseded_at = now

    entry = ProjectBranchPRStatus(
        kind=kind,
        ref=ref,
        title=title,
        status=status,
        base_ref=base_ref,
        head_ref=head_ref,
        mergeable=mergeable,
        ci_status=ci_status,
        summary=summary,
        source_id=source.id,
        recorded_by=ingested_by,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_current_branch_pr_status(db: Session, *, kind: str | None = None) -> list[ProjectBranchPRStatus]:
    stmt = select(ProjectBranchPRStatus).where(ProjectBranchPRStatus.is_current.is_(True))
    if kind is not None:
        stmt = stmt.where(ProjectBranchPRStatus.kind == kind)
    stmt = stmt.order_by(ProjectBranchPRStatus.recorded_at.desc())
    return list(db.execute(stmt).scalars())


def list_sources(db: Session, *, source_type: str | None = None, limit: int = 50) -> list[ProjectSource]:
    stmt = select(ProjectSource).order_by(ProjectSource.ingested_at.desc()).limit(limit)
    if source_type is not None:
        stmt = stmt.where(ProjectSource.source_type == source_type)
    return list(db.execute(stmt).scalars())


def latest_git_commit_source(db: Session) -> ProjectSource | None:
    stmt = (
        select(ProjectSource)
        .where(ProjectSource.source_type == "git_commit")
        .order_by(ProjectSource.ingested_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


# --- Conflict / duplicate-work detection ---------------------------------------------------


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in text.split() if len(w) > 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_conflicts_and_duplicates(db: Session, *, similarity_threshold: float = 0.4) -> dict:
    """Fas 2 requirement 6: flags POSSIBLE overlap/contradiction for human review — never
    resolves or decides anything itself. Two independent, honestly-limited checks:

      - `duplicate_work_candidates`: open blocker/next_step notes whose content shares
        significant keyword overlap (Jaccard similarity over `similarity_threshold`) — a
        heuristic signal, not a semantic understanding of the underlying work (same
        limitation app/rag/trust.py's own detect_conflicts() already documents for a
        different heuristic).
      - `data_integrity_issues`: more than one ProjectBranchPRStatus row marked
        `is_current=True` for the same (kind, ref) — should never happen if
        ingest_github_status() always supersedes correctly; surfaced as a hard signal if it
        ever does, since it means "which branch/PR status is real" is itself ambiguous.
    """
    candidate_kinds = (NoteKind.blocker, NoteKind.next_step)
    open_notes = [n for n in list_notes(db, status=NoteStatus.open) if n.kind in candidate_kinds]

    duplicate_work_candidates = []
    for i, note_a in enumerate(open_notes):
        tokens_a = _tokenize(note_a.content)
        for note_b in open_notes[i + 1 :]:
            similarity = _jaccard(tokens_a, _tokenize(note_b.content))
            if similarity >= similarity_threshold:
                duplicate_work_candidates.append(
                    {
                        "note_id_a": str(note_a.id),
                        "note_id_b": str(note_b.id),
                        "content_a": note_a.content,
                        "content_b": note_b.content,
                        "similarity": round(similarity, 2),
                    }
                )

    data_integrity_issues = []
    current = list_current_branch_pr_status(db)
    seen: dict[tuple[str, str], int] = {}
    for row in current:
        key = (row.kind, row.ref)
        seen[key] = seen.get(key, 0) + 1
    for (kind, ref), count in seen.items():
        if count > 1:
            data_integrity_issues.append({"kind": kind, "ref": ref, "current_row_count": count})

    return {
        "duplicate_work_candidates": duplicate_work_candidates,
        "data_integrity_issues": data_integrity_issues,
    }


# --- Resumption brief ------------------------------------------------------------------------


def generate_resumption_brief(
    *,
    summary: str,
    branch_name: str,
    open_pr_refs: list[str],
    open_notes: list[ProjectNote],
    branch_pr_status: list[ProjectBranchPRStatus] | None = None,
    sources: list[ProjectSource] | None = None,
) -> str:
    """Pure function: builds the exact markdown a new Claude session should be able to read
    and correctly answer, without guessing, what the project builds, what's done, what's in
    progress, what's blocked, which branch/PR applies, merge dependencies, and the exact next
    safe step (see CLAUDE.md's success metric and Fas 2 requirement 5). Every line is either a
    cited ProjectNote or a structured ProjectBranchPRStatus/ProjectSource row — never
    free-floating prose without a traceable origin."""
    branch_pr_status = branch_pr_status or []
    sources = sources or []

    lines = [
        "# MainAI Resumption Brief",
        "",
        f"**Genererad:** {datetime.utcnow().isoformat()}Z",
        f"**Aktuell branch:** `{branch_name}`",
        f"**Öppna PR:er:** {', '.join(open_pr_refs) if open_pr_refs else '(inga)'}",
        "",
        "## Huvudmål och sammanfattning",
        "",
        summary,
        "",
    ]

    fact_notes = [n for n in open_notes if n.kind == NoteKind.fact]
    lines.append("## Vad projektet bygger / aktuell fas")
    lines.append("")
    if fact_notes:
        for note in fact_notes:
            lines.append(f"- {note.content} — källa: {note.source_type} {note.source_ref}")
    else:
        lines.append("(inga registrerade statusfakta)")
    lines.append("")

    for kind, heading in (
        (NoteKind.blocker, "## Blockerare (öppna)"),
        (NoteKind.decision, "## Beslut (gällande)"),
        (NoteKind.next_step, "## Vad som pågår / nästa steg"),
        (NoteKind.uncertainty, "## Osäkerheter och konflikter"),
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

    lines.append("## Aktuell branch och PR-status")
    lines.append("")
    if branch_pr_status:
        for row in branch_pr_status:
            merge_dep = f", bas: {row.base_ref}" if row.base_ref else ""
            ci = f", CI: {row.ci_status}" if row.ci_status else ""
            lines.append(f"- **{row.kind} {row.ref}** — {row.status}{merge_dep}{ci}")
    else:
        lines.append("(ingen branch-/PR-status ingesterad ännu)")
    lines.append("")

    lines.append("## Exakt nästa säkra steg")
    lines.append("")
    next_steps = [n for n in open_notes if n.kind == NoteKind.next_step]
    if next_steps:
        lines.append(f"{next_steps[0].content} — källa: {next_steps[0].source_type} {next_steps[0].source_ref}")
    else:
        lines.append("(inget registrerat — fråga grundaren innan något nytt påbörjas)")
    lines.append("")

    lines.append("## Vad agenten INTE får göra")
    lines.append("")
    for item in PROHIBITIONS:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## Källor och tidsstämplar")
    lines.append("")
    if sources:
        for src in sources:
            lines.append(f"- {src.source_type}: {src.source_ref} (ingesterad {src.ingested_at.isoformat()}Z)")
    else:
        lines.append("(inga källor ingesterade ännu)")
    lines.append("")

    lines.append(
        "_Detta är en genererad återupptagningsbrief — varje rad ovan är spårbar till en "
        "ProjectNote-, ProjectBranchPRStatus- eller ProjectSource-rad, inte fritext utan "
        "ursprung._"
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
    """Snapshots current state: reads all currently-open notes and current branch/PR status,
    renders the resumption brief, stores it durably, and records a ProjectCheckpoint row plus
    one ProjectCheckpointNote link per open note included. Also records the latest known git
    commit SHA (if any git_commit source has been ingested) so is_checkpoint_stale() has
    something to compare against later."""
    open_notes = list_notes(db, status=NoteStatus.open)
    branch_pr_status = list_current_branch_pr_status(db)
    sources = list_sources(db, limit=20)

    brief = generate_resumption_brief(
        summary=summary,
        branch_name=branch_name,
        open_pr_refs=open_pr_refs,
        open_notes=open_notes,
        branch_pr_status=branch_pr_status,
        sources=sources,
    )
    brief_bytes = brief.encode("utf-8")

    storage = get_storage()
    reader = io.BytesIO(brief_bytes)
    blob = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=MAX_BRIEF_BYTES)

    git_source = latest_git_commit_source(db)

    checkpoint = ProjectCheckpoint(
        summary=summary,
        branch_name=branch_name,
        open_pr_refs=",".join(open_pr_refs),
        brief_storage_key=blob.storage_key,
        brief_sha256=blob.sha256,
        git_commit_sha=git_source.commit_sha if git_source else None,
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


def is_checkpoint_stale(db: Session, checkpoint: ProjectCheckpoint) -> tuple[bool, list[str]]:
    """Fas 2 requirement 4's last bullet: has anything changed since this checkpoint was
    taken? Three independent, concrete signals — never a guess:
      1. A newer git_commit source has been ingested with a different commit SHA.
      2. A note was created after the checkpoint.
      3. A branch/PR status snapshot was recorded after the checkpoint.
    """
    reasons: list[str] = []

    latest_git = latest_git_commit_source(db)
    if latest_git is not None and checkpoint.git_commit_sha and latest_git.commit_sha != checkpoint.git_commit_sha:
        reasons.append(f"Ny git-commit sedan checkpointen: {checkpoint.git_commit_sha} → {latest_git.commit_sha}")

    newer_notes = db.execute(select(ProjectNote).where(ProjectNote.created_at > checkpoint.created_at)).scalars().all()
    if newer_notes:
        reasons.append(f"{len(newer_notes)} ny(a) noteringar registrerade sedan checkpointen.")

    newer_status = (
        db.execute(select(ProjectBranchPRStatus).where(ProjectBranchPRStatus.recorded_at > checkpoint.created_at))
        .scalars()
        .all()
    )
    if newer_status:
        reasons.append(f"{len(newer_status)} ny(a) branch-/PR-statusuppdatering(ar) sedan checkpointen.")

    return (len(reasons) > 0, reasons)
