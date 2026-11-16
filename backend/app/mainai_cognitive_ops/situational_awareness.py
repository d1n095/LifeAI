"""Situational Awareness -- pure functions over caller-supplied agent/program snapshots. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

AGENT EXISTS != AGENT AVAILABLE. AGENT BUSY != ASSIGNABLE. KNOWN ACTIVE WORK != NEW TASK.
PROGRAM != SUBTASK. SUBTASK COMPLETE != PROGRAM COMPLETE. PARTIAL != COMPLETE.

This module never maintains its own agent registry or program-state store -- it reasons over
whatever snapshot the caller supplies (from `app.agent_coordination`, dev_director, etc.)."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_cognitive_ops.types import AgentState, ProgramStatus, WorkItem


def is_assignable(agent: AgentState) -> bool:
    """AGENT EXISTS != AVAILABLE, AGENT BUSY != ASSIGNABLE: an agent is assignable only when it
    exists, is not busy, and is not blocked. Idle alone does not make an agent assignable if it
    is also reported blocked (e.g. waiting on a dependency)."""

    return agent.exists and not agent.busy and not agent.blocked


def select_assignable_agents(agents: tuple[AgentState, ...]) -> tuple[AgentState, ...]:
    return tuple(a for a in agents if is_assignable(a))


@dataclass(frozen=True)
class ProgramCompletionClaim:
    program: str
    claimed_status: ProgramStatus
    reason: str


def assess_program_completion_claim(*, program: str, subtasks: tuple[WorkItem, ...]) -> ProgramCompletionClaim:
    """SUBTASK COMPLETE != PROGRAM COMPLETE: a program may only be claimed COMPLETE when every
    one of its own subtasks reports COMPLETE. One subtask reporting COMPLETE, or all but one,
    still yields PARTIAL -- never COMPLETE, and never silently rounded up."""

    if not subtasks:
        return ProgramCompletionClaim(program=program, claimed_status=ProgramStatus.ACTIVE, reason="no subtasks recorded yet for this program")

    statuses = {s.status for s in subtasks}
    if statuses == {ProgramStatus.COMPLETE}:
        return ProgramCompletionClaim(program=program, claimed_status=ProgramStatus.COMPLETE, reason=f"all {len(subtasks)} subtasks report COMPLETE")

    incomplete = [s.subtask or s.item_id for s in subtasks if s.status != ProgramStatus.COMPLETE]
    return ProgramCompletionClaim(
        program=program, claimed_status=ProgramStatus.PARTIAL,
        reason=f"{len(incomplete)} of {len(subtasks)} subtasks not yet COMPLETE: {', '.join(incomplete)} -- PARTIAL != COMPLETE",
    )


def filter_known_active_work(*, candidate_task_keywords: tuple[str, ...], active_work: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
    """KNOWN ACTIVE WORK != NEW TASK: returns the subset of `active_work` whose own keywords
    overlap with the candidate task's keywords, so a caller can check "is this already being
    worked on" before treating it as new. Overlap-only, no semantic matching -- documented
    heuristic, not NLP."""

    candidate_set = {k.lower() for k in candidate_task_keywords}
    if not candidate_set:
        return ()
    return tuple(w for w in active_work if candidate_set & {k.lower() for k in w.keywords})
