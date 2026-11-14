"""Next-task selection and minimal-sufficient-context handoff (Continuous Build Directive,
Section A/B/C: "Build a real next-task selection and handoff mechanism using existing durable
state. It must not require Founder to manually re-explain context after each agent session"
and "Implement minimal sufficient-context prompt construction.").

Deliberately built from EXISTING durable state and EXISTING functions only, never a parallel
reimplementation of either:
- `app.mainai_execution.graph.next_ready_task()` already picks the single highest-priority
  `ready` task across an owner's goals (same query this module used to duplicate -- now
  reused directly). It already covers a `retryable_failed` task whose backoff has elapsed too:
  `app.worker.py`'s `_advance_mainai_execution_retries` tick promotes those to `ready` via
  `executor.retry_task()` on every poll cycle, *before* dispatch runs on that same cycle (see
  that method's own docstring) -- so a separate "due retry" query here would just be racing an
  already-correct existing tick, not adding real coverage.
- `GithubEvidenceCandidate` (real GitHub evidence, #403) for the one source this coordinator
  genuinely adds: work nothing has planned yet.
- `ExecutionAuthorizationEnvelope` (the founder-granted scope ceiling) and
  `app.mainai_execution.approval.requires_approval()` (the existing, canonical approval-gate
  check) for the handoff's constraints section -- reused, never re-derived.

No new storage, no new authority: `select_next_task()` only READS and ranks; it never creates
a goal, claims a lease, or grants anything. Handing a GithubEvidenceCandidate to a founder as
the next thing worth reviewing is NOT the same as authorizing it -- see
app.github_evidence.service's own docstring for why that boundary is structural, not just
documented, one more time here."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution_envelopes.service import get_current_execution_envelope
from app.mainai_execution.approval import requires_approval
from app.mainai_execution.graph import next_ready_task
from app.models.github_evidence import GithubEvidenceCandidate
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskDependency

# Urgency order for surfacing an unreviewed GithubEvidenceCandidate as a proposal -- a
# merge-blocking red build is the most concrete, time-sensitive real-world fact a founder can
# act on; a merely-stale PR is the least. Never used to auto-authorize anything, only to order
# what gets SHOWN first among several unreviewed candidates.
_SIGNAL_URGENCY = ("merge_conflict", "ci_red", "draft")


@dataclass(frozen=True)
class NextTaskSelection:
    """Exactly one of `task`/`evidence_candidate` is set, or neither (nothing to do). `reason`
    is always populated, including in the nothing-to-do case -- a coordinator that found
    nothing must say so and why, never just return an empty, unexplained result."""

    task: MainAITask | None = None
    goal: MainAIGoal | None = None
    evidence_candidate: GithubEvidenceCandidate | None = None
    reason: str = ""
    source: str = "none"  # "ready_task" | "github_evidence_proposal" | "none"


def _unreviewed_evidence_candidate(db: Session, *, owner_id: uuid.UUID) -> GithubEvidenceCandidate | None:
    rows = list(
        db.execute(
            select(GithubEvidenceCandidate)
            .where(GithubEvidenceCandidate.owner_id == owner_id, GithubEvidenceCandidate.status == "unreviewed")
            .order_by(GithubEvidenceCandidate.observed_at.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    def _urgency(row: GithubEvidenceCandidate) -> int:
        for rank, signal in enumerate(_SIGNAL_URGENCY):
            if signal in (row.signals or []):
                return rank
        return len(_SIGNAL_URGENCY)

    return min(rows, key=_urgency)


def select_next_task(db: Session, *, owner_id: uuid.UUID) -> NextTaskSelection:
    """Priority order, each step a real, independently-checkable read of existing durable
    state -- never a guess:

    1. The highest-priority task `app.mainai_execution.graph.next_ready_task()` already picks
       for dispatch across this owner's goals. This single existing function already covers a
       `retryable_failed` task whose backoff has elapsed too -- see this module's docstring for
       why that isn't duplicated here.
    2. The highest-urgency unreviewed GithubEvidenceCandidate, surfaced as a PROPOSAL for the
       founder/an authorized caller to review -- never auto-selected as if it were already
       approved work; `source` says so explicitly.
    3. Nothing to do.
    """
    ready = next_ready_task(db, owner_id=owner_id)
    if ready is not None:
        goal = db.get(MainAIGoal, ready.goal_id)
        return NextTaskSelection(
            task=ready, goal=goal, source="ready_task",
            reason=f"task {ready.id} is ready (dependencies satisfied) and is the highest-priority ready task",
        )

    candidate = _unreviewed_evidence_candidate(db, owner_id=owner_id)
    if candidate is not None:
        return NextTaskSelection(
            evidence_candidate=candidate, source="github_evidence_proposal",
            reason=(
                f"no task is ready for dispatch; {candidate.source_kind} "
                f"{candidate.source_repo}#{candidate.source_number} is the highest-urgency unreviewed "
                f"GitHub evidence candidate ({', '.join(candidate.signals) or 'no signals'}) -- "
                "this is a PROPOSAL for review, not authorized work"
            ),
        )

    return NextTaskSelection(reason="no ready task and no unreviewed GitHub evidence candidate", source="none")


@dataclass(frozen=True)
class TaskHandoffPrompt:
    """The minimal-sufficient-context handoff -- every field independently traceable to a real
    row, never free prose summarizing "everything so far". `missing_information` holds
    questions ONLY when something a worker genuinely needs is actually absent; an empty list
    means the task is fully specified, not that nothing was checked."""

    objective: str
    evidence: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Assembles the final prompt text -- only the sections that have content, never an
        empty "Acceptance criteria:" heading followed by nothing, and never a generic "Next,
        you should..." filler section."""
        parts = [f"Objective: {self.objective}"]
        if self.evidence:
            parts.append("Evidence:\n" + "\n".join(f"- {e}" for e in self.evidence))
        if self.constraints:
            parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.acceptance_criteria:
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in self.acceptance_criteria))
        if self.missing_information:
            parts.append("Before starting, please answer:\n" + "\n".join(f"- {q}" for q in self.missing_information))
        return "\n\n".join(parts)


def build_task_handoff_prompt(db: Session, *, task: MainAITask, goal: MainAIGoal) -> TaskHandoffPrompt:
    """Builds the handoff from exactly the durable rows a worker needs, so a founder never has
    to re-explain context a prior session already established durably."""
    evidence: list[str] = [f"Goal: {goal.title}"]

    deps = list(
        db.execute(select(MainAITaskDependency).where(MainAITaskDependency.task_id == task.id)).scalars().all()
    )
    if deps:
        evidence.append(f"Depends on {len(deps)} already-completed task(s): {sorted(str(d.depends_on_task_id) for d in deps)}")

    linked_candidate = db.execute(
        select(GithubEvidenceCandidate).where(GithubEvidenceCandidate.authorized_goal_id == goal.id)
    ).scalar_one_or_none()
    if linked_candidate is not None:
        evidence.append(
            f"Originates from {linked_candidate.source_kind} {linked_candidate.source_repo}#{linked_candidate.source_number} "
            f"({linked_candidate.source_url}), signals observed: {', '.join(linked_candidate.signals) or 'none'}"
        )

    envelope = get_current_execution_envelope(db, owner_id=task.owner_id, goal_id=goal.id)
    if envelope is not None:
        evidence.append(f"Authorized repository scope: {envelope.repository_identity}, paths: {envelope.authorized_paths}")

    task_needs_approval = requires_approval(
        policy_name=goal.approval_policy, task_type=task.task_type, task_approval_required=task.approval_required
    )
    constraints: list[str] = [
        f"Approval policy: {goal.approval_policy}",
        f"Task type '{task.task_type}' {'requires explicit founder approval before dispatch' if task_needs_approval else 'does not structurally require approval'}"
        + (" (per-task approval_required=True overrides the policy default)" if task.approval_required else ""),
        f"Risk level: {task.risk_level.value}",
    ]

    missing_information: list[str] = []
    if not task.verification_plan:
        missing_information.append(
            "No acceptance criteria are recorded for this task (verification_plan is empty) -- "
            "what should verification check before this is considered done?"
        )
    if goal.approval_policy == "autonomous_development_work" and envelope is None:
        missing_information.append(
            f"Goal uses the '{goal.approval_policy}' policy, but no active execution authorization envelope "
            "exists for it -- founder authorization is needed before autonomous work can proceed."
        )

    return TaskHandoffPrompt(
        objective=task.description,
        evidence=evidence,
        constraints=constraints,
        acceptance_criteria=[str(item) for item in task.verification_plan],
        missing_information=missing_information,
    )
