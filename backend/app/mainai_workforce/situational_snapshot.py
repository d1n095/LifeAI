"""Real composition with `app.agent_coordination.runtime_view` + `app.mainai_cognitive_ops`.
See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Closes the situational-awareness P1 disclosed in `HANDOFF_CLAUDE_COGNITIVE_OPS.md`: this
module converts the REAL `AgentRuntimeView`/`RuntimeStatus` (the durable, live agent/work-
assignment registry `app.agent_coordination` already owns) into
`app.mainai_cognitive_ops.types.AgentState` -- never a second agent registry. `WorkItem`
conversion reuses `AssignmentRuntimeView`'s own real branch/sha/goal/task fields, satisfying
the founder's own §15 requirement to know "what exact branch/SHA each agent owns" without
duplicating that data anywhere."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import AgentRuntimeView, RuntimeStatus, all_agents_runtime_snapshot
from app.mainai_cognitive_ops.types import AgentState, ProgramStatus, WorkItem

_BUSY_STATUSES = frozenset({RuntimeStatus.RUNNING, RuntimeStatus.REVIEWING})
_BLOCKED_STATUSES = frozenset({RuntimeStatus.WAITING_DEPENDENCY, RuntimeStatus.WAITING_REVIEW, RuntimeStatus.BLOCKED, RuntimeStatus.OFFLINE})
_IDLE_STATUSES = frozenset({RuntimeStatus.IDLE, RuntimeStatus.COMPLETED, RuntimeStatus.FAILED})


def agent_runtime_view_to_agent_state(view: AgentRuntimeView) -> AgentState:
    current_task = None
    current_program = None
    if view.current_assignments:
        first = view.current_assignments[0]
        current_task = str(first.task_id) if first.task_id else str(first.assignment_id)
        current_program = str(first.goal_id)

    return AgentState(
        agent_id=view.agent_key,
        exists=True,
        busy=view.runtime_status in _BUSY_STATUSES,
        blocked=view.runtime_status in _BLOCKED_STATUSES,
        idle=view.runtime_status in _IDLE_STATUSES,
        current_program=current_program,
        current_task=current_task,
    )


def real_agent_states_snapshot(db: Session, *, owner_id: uuid.UUID) -> tuple[AgentState, ...]:
    """The real, live composition a caller uses instead of hand-building `AgentState` tuples --
    `app.mainai_cognitive_ops.situational_awareness.select_assignable_agents()` then applies
    unchanged to THIS real snapshot."""

    views = all_agents_runtime_snapshot(db, owner_id=owner_id)
    return tuple(agent_runtime_view_to_agent_state(v) for v in views)


def assignment_to_work_item(view) -> WorkItem:
    """One real `AssignmentRuntimeView` (already carries branch/sha/goal/task, see
    `app.agent_coordination.runtime_view`'s own docstring) as a `WorkItem` for
    `app.mainai_cognitive_ops.duplication_control`/`situational_awareness` to reason over."""

    status = ProgramStatus.COMPLETE if view.runtime_status.value in ("COMPLETED",) else (
        ProgramStatus.PARTIAL if view.runtime_status.value == "FAILED" else ProgramStatus.ACTIVE
    )
    keywords = tuple(p for p in (view.branch, view.repository_identity) if p)
    return WorkItem(
        item_id=str(view.assignment_id), program=str(view.goal_id), subtask=str(view.task_id) if view.task_id else None,
        owner_agent=view.agent_key, status=status, keywords=keywords,
    )
