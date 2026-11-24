"""Founder Development Intelligence -- READ-ONLY synthesis over the existing agent-coordination
runtime view (migration 0046) and recorded dispatch evidence (app.intelligence_governance).
Classifies each active work assignment into a plain-language founder status, surfaces its most
recently recorded CI conclusion (if any dispatch has reported one), and flags duplicate-work risk
using the EXISTING structural check (`is_duplicate_canonical_work`) -- never a second detector.

This module adds no new table, dispatches nothing, executes nothing, and writes nothing to the
repository or to GitHub. It is a view over state that already exists, exactly like
`app.agent_coordination.runtime_view` and `app.github_status.service` it builds on.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import AssignmentRuntimeView, RuntimeStatus, assignment_runtime_view
from app.agent_coordination.service import TERMINAL_WORK_ASSIGNMENT_STATUSES
from app.models.agent_coordination import AgentWorkAssignment, CoordinationAgent
from app.models.intelligence_governance import IntelligenceEvidence

# A finished assignment matters to "who is WORKING / BLOCKED / FINISHED" for a while after it
# actually finishes -- `work_registry_snapshot()` (the existing Runtime Control Plane this
# reuses for everything ELSE) deliberately excludes every terminal assignment, by design, as
# documented in its own module docstring ("what is every agent doing RIGHT NOW"), which is
# correct for a live control-plane view but would make "who is finished" permanently empty
# here. This window is the one deliberate departure from pure reuse: terminal assignments stay
# visible for this long after their `updated_at`, then age out -- never forever (this is a
# status surface, not a permanent audit log; `AgentWorkAssignmentEvent` already is that).
_RECENTLY_FINISHED_WINDOW = timedelta(hours=24)

# Swedish, matching this codebase's existing founder-facing-text convention (see
# app.github_status.service's own Swedish strings). A deliberately TOTAL mapping -- every
# RuntimeStatus value has an entry, so a future enum member fails loudly (KeyError) rather than
# silently falling through to the raw enum name.
_PLAIN_LANGUAGE: dict[RuntimeStatus, str] = {
    RuntimeStatus.IDLE: "Väntar på att bli upptagen",
    RuntimeStatus.RUNNING: "Arbetar just nu",
    RuntimeStatus.WAITING_DEPENDENCY: "Väntar på en annan uppgift",
    RuntimeStatus.WAITING_REVIEW: "Klar, väntar på granskning",
    RuntimeStatus.REVIEWING: "Granskas just nu",
    RuntimeStatus.BLOCKED: "Blockerad",
    RuntimeStatus.COMPLETED: "Klar",
    RuntimeStatus.FAILED: "Misslyckades",
    RuntimeStatus.OFFLINE: "Inte tillgänglig",
}


@dataclass(frozen=True)
class FounderAssignmentSummary:
    """One assignment's founder-facing summary. `signature` is a stable hash of every field a
    founder would actually notice changing; a caller that already showed a given signature can
    skip re-showing it (see `founder_development_summary`'s `known_signatures` parameter) --
    this is the whole of this module's redundant-update suppression, deliberately stateless: no
    new table records "what was last shown", the caller holds that if it wants to."""

    assignment_id: uuid.UUID
    agent_key: str
    plain_status: str
    canonical_status: str
    branch: str | None
    head_sha: str | None
    upstream_pr_ref: str | None
    ci_conclusion: str | None
    block_explanation: str | None
    duplicate_warning: str | None
    signature: str


def _signature(*parts: str | None) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _latest_dispatch_evidence(db: Session, *, intelligence_execution_id: uuid.UUID | None) -> dict | None:
    """The most recently recorded `dispatch_result` evidence for this assignment's execution,
    if any -- this is where `ci_conclusion`/`tests_passed` etc. actually live today (see
    `app.agent_coordination.dispatch.apply_dispatch_result`'s call to
    `record_assignment_outcome`); nothing here duplicates that recording, it only reads it
    back. `None` when no dispatch has ever reported outcome evidence yet -- a real, honest
    "unknown", never guessed."""

    if intelligence_execution_id is None:
        return None
    row = (
        db.execute(
            select(IntelligenceEvidence)
            .where(
                IntelligenceEvidence.execution_id == intelligence_execution_id,
                IntelligenceEvidence.evidence_kind == "dispatch_result",
            )
            .order_by(IntelligenceEvidence.observed_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return row.payload if row is not None else None


def _duplicate_warning(db: Session, assignment: AgentWorkAssignment) -> str | None:
    """Same identity rule as the EXISTING structural duplicate-work check
    (`app.agent_coordination.service.is_duplicate_canonical_work`): same owner, same
    `canonical_work_key`, any other non-terminal assignment, skipped entirely when this
    assignment sits in an explicit parallel-exploration group. Not called directly, because
    that function has no self-exclusion parameter and is only ever used PRE-INSERT (before the
    assignment being checked exists in the table) -- calling it post-insert, as this read-only
    view does for an assignment that already exists, would make every assignment match itself.
    Never a second, independently-derived notion of "duplicate" -- same key, same terminal
    set, same parallel-exploration carve-out, just with `!= assignment.id` added for the
    post-insert case."""

    if assignment.parallel_exploration_group_id is not None:
        return None
    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    duplicate = (
        db.execute(
            select(AgentWorkAssignment).where(
                AgentWorkAssignment.owner_id == assignment.owner_id,
                AgentWorkAssignment.canonical_work_key == assignment.canonical_work_key,
                AgentWorkAssignment.status.notin_(terminal),
                AgentWorkAssignment.id != assignment.id,
            )
        )
        .scalars()
        .first()
    )
    if duplicate is None:
        return None
    return f"Detta arbete pågår redan eller är redan klart i en annan tilldelning ({duplicate.id}) — kontrollera innan ny kod skrivs."


def summarize_assignment(db: Session, view: AssignmentRuntimeView, assignment: AgentWorkAssignment) -> FounderAssignmentSummary:
    evidence = _latest_dispatch_evidence(db, intelligence_execution_id=assignment.intelligence_execution_id)
    ci_conclusion = evidence.get("ci_conclusion") if evidence else None
    plain_status = _PLAIN_LANGUAGE[view.runtime_status]
    block_explanation = f"{plain_status}: {view.block_reason}" if view.block_reason else None
    duplicate_warning = _duplicate_warning(db, assignment)
    signature = _signature(
        view.canonical_status,
        view.head_sha,
        view.upstream_pr_ref,
        ci_conclusion,
        view.block_reason,
        duplicate_warning,
    )
    return FounderAssignmentSummary(
        assignment_id=view.assignment_id,
        agent_key=view.agent_key,
        plain_status=plain_status,
        canonical_status=view.canonical_status,
        branch=view.branch,
        head_sha=view.head_sha,
        upstream_pr_ref=view.upstream_pr_ref,
        ci_conclusion=ci_conclusion,
        block_explanation=block_explanation,
        duplicate_warning=duplicate_warning,
        signature=signature,
    )


def founder_development_summary(
    db: Session, *, owner_id: uuid.UUID, known_signatures: dict[str, str] | None = None
) -> list[FounderAssignmentSummary]:
    """Every assignment that's either still active OR finished within
    `_RECENTLY_FINISHED_WINDOW`, for this owner -- "who is working, who is blocked, who is
    finished" needs all three, so (unlike `work_registry_snapshot`, which this module reuses
    for everything else) this query does NOT drop terminal rows outright, only old ones.
    `known_signatures` (a map of assignment-id string to the signature the caller last showed)
    is optional; a row whose signature is unchanged is OMITTED from the result entirely -- the
    caller sees exactly the delta, never a repeated "nothing changed" entry. Passing nothing
    returns every row."""

    known_signatures = known_signatures or {}
    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    cutoff = datetime.utcnow() - _RECENTLY_FINISHED_WINDOW
    assignments = list(
        db.execute(
            select(AgentWorkAssignment)
            .where(
                AgentWorkAssignment.owner_id == owner_id,
                (AgentWorkAssignment.status.notin_(terminal)) | (AgentWorkAssignment.updated_at >= cutoff),
            )
            .order_by(AgentWorkAssignment.updated_at.desc())
        ).scalars()
    )
    if not assignments:
        return []

    agent_ids = {a.agent_id for a in assignments}
    agents_by_id: dict[uuid.UUID, CoordinationAgent] = {
        a.id: a for a in db.execute(select(CoordinationAgent).where(CoordinationAgent.id.in_(agent_ids))).scalars()
    }

    out: list[FounderAssignmentSummary] = []
    for assignment in assignments:
        view = assignment_runtime_view(db, assignment, agent=agents_by_id.get(assignment.agent_id))
        summary = summarize_assignment(db, view, assignment)
        if known_signatures.get(str(summary.assignment_id)) == summary.signature:
            continue
        out.append(summary)
    return out
