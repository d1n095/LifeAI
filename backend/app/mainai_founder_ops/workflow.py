"""Dependency graph, independent ready work, and next actions from durable rows.

Does not invent activity. A ready sibling while another task waits on CI is surfaced as
independent work that a worker may claim — it is not reported as running.
Circular dependencies are fail-closed blockers, never silently ignored.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import (
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)

WAITING_STATUSES = frozenset(
    {
        MainAITaskStatus.pending,
        MainAITaskStatus.blocked,
        MainAITaskStatus.waiting_external,
        MainAITaskStatus.waiting_ci,
    }
)
READY_STATUSES = frozenset({MainAITaskStatus.ready})


def load_dependency_edges(db: Session, *, owner_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID]]:
    rows = db.execute(
        select(MainAITaskDependency.task_id, MainAITaskDependency.depends_on_task_id).where(
            MainAITaskDependency.owner_id == owner_id
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def detect_dependency_cycles(edges: list[tuple[uuid.UUID, uuid.UUID]]) -> list[list[str]]:
    """Three-color DFS. Returns distinct cycles as lists of task id strings."""
    adjacency: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task_id, depends_on in edges:
        adjacency.setdefault(task_id, []).append(depends_on)
        adjacency.setdefault(depends_on, [])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in adjacency}
    path: list[uuid.UUID] = []
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(node: uuid.UUID) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in adjacency.get(node, []):
            if color[neighbor] == GRAY:
                cycle_start = path.index(neighbor)
                cycle_ids = [str(n) for n in path[cycle_start:] + [neighbor]]
                key = tuple(sorted(set(cycle_ids)))
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle_ids)
            elif color[neighbor] == WHITE:
                visit(neighbor)
        path.pop()
        color[node] = BLACK

    for node in adjacency:
        if color[node] == WHITE:
            visit(node)
    return cycles


def task_ids_in_cycles(cycles: list[list[str]]) -> set[str]:
    return {task_id for cycle in cycles for task_id in cycle}


def independent_ready_work(tasks: Iterable[MainAITask]) -> list[dict[str, Any]]:
    """Ready tasks whose siblings wait on a dependency or CI.

    These may proceed without waiting for the blocked sibling. They are not running until a
    worker claims them. Never duplicates a second description of the same task.
    """
    by_goal: dict[uuid.UUID, list[MainAITask]] = {}
    for task in tasks:
        by_goal.setdefault(task.goal_id, []).append(task)

    ready_items: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for goal_id, group in by_goal.items():
        waiting = [t for t in group if t.status in WAITING_STATUSES]
        ready = [t for t in group if t.status in READY_STATUSES]
        if not waiting or not ready:
            continue
        waiting_ids = [str(t.id) for t in waiting]
        for task in ready:
            if task.id in seen:
                continue
            seen.add(task.id)
            ready_items.append(
                {
                    "task_id": str(task.id),
                    "goal_id": str(goal_id),
                    "description": task.description,
                    "founder_status": "queued",
                    "running": False,
                    "invented_activity": False,
                    "waiting_sibling_ids": waiting_ids,
                    "note": "Ready while siblings wait. Not running until a worker claims it.",
                }
            )
    return ready_items


def approval_already_granted(events: Iterable[MainAITaskEvent] | None) -> bool:
    if not events:
        return False
    return any(e.event_type == MainAITaskEventType.approval_granted for e in events)


def pending_approval_tasks(
    tasks: Iterable[MainAITask],
    events_by_task: dict[uuid.UUID, list[MainAITaskEvent]],
) -> list[MainAITask]:
    pending: list[MainAITask] = []
    for task in tasks:
        if not task.approval_required:
            continue
        if approval_already_granted(events_by_task.get(task.id)):
            continue
        pending.append(task)
    return pending


def build_next_actions(
    *,
    stop: dict[str, Any],
    cycles: list[list[str]],
    pending_approvals: list[MainAITask],
    independent_ready: list[dict[str, Any]],
    failed_items: list[dict[str, Any]],
    authorization_open: int,
) -> list[dict[str, Any]]:
    """Human-readable next steps from durable state. Never a fabricated progress narrative."""
    actions: list[dict[str, Any]] = []
    if stop.get("blocked"):
        actions.append(
            {
                "kind": "clear_stop",
                "priority": 0,
                "detail": stop.get("code") or "STOP is blocking all privileged work.",
            }
        )
    for cycle in cycles:
        actions.append(
            {
                "kind": "break_circular_dependency",
                "priority": 1,
                "task_ids": cycle,
                "detail": "Stored dependency cycle. No task in the cycle can become ready.",
            }
        )
    for task in pending_approvals:
        actions.append(
            {
                "kind": "founder_approval",
                "priority": 2,
                "task_id": str(task.id),
                "description": task.description,
                "detail": "Privileged step still requires explicit Founder authorization.",
            }
        )
    if authorization_open and not pending_approvals:
        actions.append(
            {
                "kind": "review_open_authorization",
                "priority": 2,
                "detail": f"{authorization_open} open authorization request(s) are not yet granted.",
            }
        )
    for item in independent_ready:
        actions.append(
            {
                "kind": "continue_independent_ready",
                "priority": 3,
                "task_id": item["task_id"],
                "description": item["description"],
                "detail": item["note"],
                "invented_activity": False,
            }
        )
    for item in failed_items[:10]:
        actions.append(
            {
                "kind": "inspect_failure",
                "priority": 4,
                "id": item.get("task_id") or item.get("job_id") or item.get("assignment_id"),
                "source": item.get("source"),
                "detail": item.get("failure_class") or item.get("source_status") or "failed",
            }
        )
    return actions


def dependency_records(
    db: Session,
    *,
    owner_id: uuid.UUID,
    tasks_by_id: dict[uuid.UUID, MainAITask],
) -> list[dict[str, Any]]:
    edges = load_dependency_edges(db, owner_id=owner_id)
    records: list[dict[str, Any]] = []
    for task_id, depends_on in edges:
        task = tasks_by_id.get(task_id)
        dep = tasks_by_id.get(depends_on)
        records.append(
            {
                "task_id": str(task_id),
                "depends_on_task_id": str(depends_on),
                "task_status": task.status.value if task is not None else "unknown",
                "depends_on_status": dep.status.value if dep is not None else "unknown",
                "task_description": task.description if task is not None else None,
                "depends_on_description": dep.description if dep is not None else None,
            }
        )
    return records
