"""Life MainAI Intelligence program -- GitHub evidence staging (Continuous Build Directive,
Section B). The staging layer between real, verified GitHub evidence (app.safe_planner.
github_work_queue.build_github_work_queue_report()) and real, governed MainAI work
(app.mainai_execution.planner.create_goal()). See migration 0069_github_evidence_staging's own
module docstring, and app.models.github_evidence.GithubEvidenceCandidate's docstring, for the
full architecture and why this is a separate table from app.work_candidates rather than a reuse
of it.

Hard rule, structural not just documented: `record_github_evidence_candidates()` NEVER creates
a `MainAIGoal`, directly or indirectly. The ONLY function in this module that can is
`authorize_github_evidence_candidate()`, and it ALWAYS requires the caller to supply
`authorized_by` explicitly -- a PR/issue existing, or even looking urgent (ci_red, stale), is
never silently treated as founder authorization. `authorize_github_evidence_candidate()` does
not reimplement goal creation -- it calls `create_goal()`, the exact same function every other
real goal already goes through, so this module only decides WHETHER to call it and WITH WHAT
ARGUMENTS, never WHETHER THE CALLER IS ALLOWED TO."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.planner import create_goal
from app.models.github_evidence import GithubEvidenceCandidate
from app.models.mainai_execution import MainAIGoal, MainAIGoalRiskLevel
from app.safe_planner.github_work_queue import WorkQueueItem


class GithubEvidenceError(ValueError):
    pass


def candidate_idempotency_key(*, source_repo: str, source_kind: str, source_number: int) -> str:
    """Deterministic identity for ONE real GitHub PR/issue -- deliberately not a random UUID,
    so recording the SAME item twice (the normal case: every scheduled evidence scan sees the
    same open PRs again) resolves to the same row, never a duplicate."""
    return f"{source_repo}:{source_kind}:{source_number}"


def _same_identity(row: GithubEvidenceCandidate, item: WorkQueueItem, source_repo: str) -> bool:
    return row.source_repo == source_repo and row.source_kind == item.kind and row.source_number == item.number


def record_github_evidence_candidates(
    db: Session, *, owner_id: uuid.UUID, source_repo: str, items: list[WorkQueueItem]
) -> list[GithubEvidenceCandidate]:
    """Records evidence for every item in a `build_github_work_queue_report()` result --
    never a claim that any of it is authorized, only that a signal producer observed real
    GitHub state. Idempotent per `candidate_idempotency_key()`: a PR/issue seen again updates
    its own mutable OBSERVATION fields (title/signals/source_url/observed_at) in place --
    unlike `app.work_candidates.service`'s stricter `_same()` (which REJECTS a reused key
    whose fields differ), this is deliberate here: live CI/merge/staleness signals for the
    SAME PR are EXPECTED to change between scans, unlike a WorkCandidate's one-time classifier
    output. `status`/`authorized_goal_id`/`dismissed_reason` are a founder decision and are
    NEVER touched here, regardless of what the fresh evidence says -- only `authorize_github_
    evidence_candidate()`/`dismiss_github_evidence_candidate()` can change them. Never raises
    for "this item looks like noise" -- that judgment happens later, explicitly."""
    rows: list[GithubEvidenceCandidate] = []
    for item in items:
        key = candidate_idempotency_key(source_repo=source_repo, source_kind=item.kind, source_number=item.number)
        existing = db.execute(
            select(GithubEvidenceCandidate).where(
                GithubEvidenceCandidate.owner_id == owner_id, GithubEvidenceCandidate.idempotency_key == key
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not _same_identity(existing, item, source_repo):
                raise GithubEvidenceError(f"idempotency key {key!r} reused for a different GitHub item")
            existing.title = item.title
            existing.signals = list(item.signals)
            existing.source_url = item.html_url
            existing.observed_at = datetime.utcnow()  # noqa: DTZ003 -- matches this table's own naive-UTC convention
            existing.updated_at = datetime.utcnow()  # noqa: DTZ003
            rows.append(existing)
            continue
        row = GithubEvidenceCandidate(
            owner_id=owner_id,
            source_repo=source_repo,
            source_kind=item.kind,
            source_number=item.number,
            source_url=item.html_url,
            title=item.title,
            signals=list(item.signals),
            status="unreviewed",
            idempotency_key=key,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def authorize_github_evidence_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    candidate_id: uuid.UUID,
    authorized_by: str,
    title: str | None = None,
    original_instruction: str | None = None,
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low,
    approval_policy: str = "standard_repo_work",
) -> tuple[GithubEvidenceCandidate, MainAIGoal]:
    """The ONLY path from a GitHub evidence candidate to real, executable MainAI work.
    `authorized_by` is ALWAYS the caller's own explicit assertion -- this function has no
    default and never reads it off the candidate's own signals. Delegates the actual
    `MainAIGoal` row to `create_goal()` itself, exactly like `app.work_candidates.service.
    authorize_work_candidate()` does, so any future change to goal-creation semantics applies
    here automatically, not as a second, diverging implementation."""
    row = db.execute(
        select(GithubEvidenceCandidate)
        .where(GithubEvidenceCandidate.id == candidate_id, GithubEvidenceCandidate.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise GithubEvidenceError("github evidence candidate is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise GithubEvidenceError(f"github evidence candidate is already {row.status}, not unreviewed")

    goal = create_goal(
        db,
        owner_id=owner_id,
        title=title or row.title,
        original_instruction=original_instruction or f"{row.source_kind} {row.source_repo}#{row.source_number}: {row.title}",
        created_by=authorized_by,
        risk_level=risk_level,
        approval_policy=approval_policy,
    )
    db.flush()
    row.status = "authorized"
    row.authorized_goal_id = goal.id
    row.updated_at = datetime.utcnow()  # noqa: DTZ003
    db.flush()
    return row, goal


def dismiss_github_evidence_candidate(db: Session, *, owner_id: uuid.UUID, candidate_id: uuid.UUID, reason: str) -> GithubEvidenceCandidate:
    """Marks a candidate as deliberately not worth acting on -- a founder/authorized decision,
    recorded with its own reason, never a silent delete (the row stays as a record that this
    evidence WAS reviewed, same as app.work_candidates.service.dismiss_work_candidate())."""
    row = db.execute(
        select(GithubEvidenceCandidate)
        .where(GithubEvidenceCandidate.id == candidate_id, GithubEvidenceCandidate.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise GithubEvidenceError("github evidence candidate is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise GithubEvidenceError(f"github evidence candidate is already {row.status}, not unreviewed")
    row.status = "dismissed"
    row.dismissed_reason = reason
    row.updated_at = datetime.utcnow()  # noqa: DTZ003
    db.flush()
    return row


def get_github_evidence_candidate(db: Session, *, owner_id: uuid.UUID, candidate_id: uuid.UUID) -> GithubEvidenceCandidate | None:
    return db.execute(
        select(GithubEvidenceCandidate).where(
            GithubEvidenceCandidate.id == candidate_id, GithubEvidenceCandidate.owner_id == owner_id
        )
    ).scalar_one_or_none()


def list_github_evidence_candidates(db: Session, *, owner_id: uuid.UUID, status: str | None = None) -> list[GithubEvidenceCandidate]:
    stmt = select(GithubEvidenceCandidate).where(GithubEvidenceCandidate.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(GithubEvidenceCandidate.status == status)
    return list(db.execute(stmt.order_by(GithubEvidenceCandidate.observed_at)).scalars().all())
