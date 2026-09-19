"""Real WIP / workload awareness over the LIVE `WorkforceAssignment` (`app.workforce`) and
`MainAITask` (`app.mainai_execution`) runtimes -- NOT the frozen, unreviewed `dev_director`
candidate. MORE PARALLELISM != MORE PROGRESS enforced via a real, documented ceiling read from
ACTUAL current load, never a guess.

Deliberately read-only: nothing here creates, cancels, reassigns, or defers any real work.
`should_defer_new_work()` only RECOMMENDS -- a caller that decides to defer expresses that
decision through whatever it already uses to not create new work (e.g. simply not calling
`app.work_candidates.service.authorize_work_candidate()`); this module has no mutating call
anywhere in it."""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import TERMINAL_MAINAI_TASK_STATUSES, MainAITask, MainAITaskStatus
from app.models.workforce import WorkforceAssignment

# WorkforceAssignment.status real vocabulary, confirmed by direct reading of
# app.workforce.broker's own transitions: "assigned" (resolve_delegation), "awaiting_
# verification" (after dispatch), "completed"/"failed"/"superseded" (record_verification_
# outcome / supersede paths), and "revoked" (app.workforce.authority.
# revoke_assignment_authority(), called by cancel_assignment()). Not a DB-CHECK-enforced
# closed vocabulary today (unlike MainAITaskStatus's real Postgres Enum) -- treated here as
# documentation, matching app.workforce.types's own "extensible string vocabularies"
# convention. Only "assigned"/"awaiting_verification" count as in-flight capacity; every
# other real status (including "revoked") is excluded by construction, not by the extra
# `revoked_at is None` check alone (belt-and-suspenders: revoke_assignment_authority() sets
# BOTH status="revoked" AND revoked_at in the same call).
ASSIGNMENT_IN_FLIGHT_STATUSES = frozenset({"assigned", "awaiting_verification"})
ASSIGNMENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "superseded", "revoked"})

# MainAITaskStatus values NOT in TERMINAL_MAINAI_TASK_STATUSES (app.models.mainai_execution)
# -- i.e. still consuming attention/capacity in some form, whether actively running or
# blocked/waiting on something that will eventually need attention again. Deliberately
# INCLUDES retryable_failed (not yet given up on) -- excludes only completed/failed/cancelled.
TASK_IN_FLIGHT_STATUSES = frozenset(s.value for s in MainAITaskStatus) - frozenset(
    s.value for s in TERMINAL_MAINAI_TASK_STATUSES
)


def default_wip_limit() -> int:
    """A small, deliberately conservative ceiling on concurrent in-flight MainAITask rows per
    owner. Not derived from any measured throughput data (none exists yet in this codebase) --
    a documented, easily-overridden starting point, matching this codebase's other
    conservative-default conventions (e.g. app.mainai_executive.bounds.ExecutiveScanBounds's
    own hand-picked scan bounds). Callers needing a different ceiling should pass their own
    `wip_limit` to should_defer_new_work() -- never patch this constant per call site."""
    return 5


def current_wip_load(db: Session, *, owner_id: uuid.UUID) -> dict:
    """Real counts by status, read fresh from the LIVE WorkforceAssignment and MainAITask
    tables for this owner -- never cached, never estimated, never read from the frozen
    dev_director candidate."""
    assignment_rows = db.execute(
        select(WorkforceAssignment.status, WorkforceAssignment.revoked_at).where(
            WorkforceAssignment.owner_id == owner_id
        )
    ).all()
    assignment_counts = Counter(status for status, _revoked_at in assignment_rows)
    assignments_in_flight = sum(
        1
        for status, revoked_at in assignment_rows
        if status in ASSIGNMENT_IN_FLIGHT_STATUSES and revoked_at is None
    )

    task_status_rows = db.execute(select(MainAITask.status).where(MainAITask.owner_id == owner_id)).scalars().all()
    task_status_values = [s.value if hasattr(s, "value") else s for s in task_status_rows]
    task_counts = Counter(task_status_values)
    tasks_in_flight = sum(1 for value in task_status_values if value in TASK_IN_FLIGHT_STATUSES)
    tasks_running = task_counts.get(MainAITaskStatus.running.value, 0)

    return {
        "owner_id": str(owner_id),
        "assignment_counts_by_status": dict(assignment_counts),
        "assignments_in_flight": assignments_in_flight,
        "task_counts_by_status": dict(task_counts),
        "tasks_in_flight": tasks_in_flight,
        "tasks_running": tasks_running,
        "authority_impact": "NONE",
    }


def should_defer_new_work(
    db: Session, *, owner_id: uuid.UUID, wip_limit: int | None = None
) -> tuple[bool, str]:
    """Deterministic: never itself defers/cancels/blocks anything -- returns a
    (should_defer, reason) signal for a caller (e.g. Part 2's judgment.py) to act on. Bases
    the decision on REAL current in-flight MainAITask load (the actual dispatched unit of
    work), not on WorkforceAssignment counts alone -- an assignment can still read 'assigned'
    while its underlying task has already raced to completion; MainAITask.status is the more
    current signal for whether real capacity is actually occupied right now."""
    limit = wip_limit if wip_limit is not None else default_wip_limit()
    if limit < 1:
        raise ValueError("wip_limit must be at least 1")
    load = current_wip_load(db, owner_id=owner_id)
    in_flight = load["tasks_in_flight"]
    if in_flight >= limit:
        return True, (
            f"{in_flight} MainAITask row(s) already in flight for this owner (limit={limit}); "
            "defer creating new work until capacity frees up"
        )
    return False, f"{in_flight}/{limit} MainAITask row(s) in flight -- capacity available"
