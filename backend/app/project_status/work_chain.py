"""Founder Alpha R15: the live work chain, composed from the systems that already own it.

R14 taught the founder's project memory to see *the plan* (blueprint, buckets, GitHub). It could
still not see *the work*: a goal one approval away from running, a task stuck on a dependency, a
`task_execution` job whose lease died, an agent assignment nobody has moved since yesterday, a
work candidate waiting for a decision. Those facts already exist in
`mainai_goals`/`mainai_tasks`/`mainai_jobs`/`agent_work_assignments`/`work_candidates` — this
module only READS them and says so, with the same honesty rules as R14:

  - nothing here writes, dispatches, retries, cancels or approves anything. There is no
    `db.add`, no commit, no service call that mutates. The only place work is authorized
    remains `app.mainai_execution.approval.require_task_approval()` and the founder-only
    routers that reach it.
  - "waiting for approval" is computed with the SAME policy function the dispatch gate uses
    (`requires_approval`) against the SAME `approval_granted` event, so this list can never
    disagree with what dispatch would actually do.
  - a stale job is REPORTED, never stopped: the worker's own lease/liveness machinery is the
    only thing allowed to reclaim one (see `docs/MAINAI_JOB_RUNTIME.md`).
  - unknown is not zero. A job with no lease row, a task with no recorded blocker, an agent
    with no heartbeat are each stated as what they are.

Linking is explicit and provenance-carrying: every entry names the row it came from
(`mainai_goals:<id>`, `mainai_jobs:<id>`), so the UI and the conversation can cite a real
record instead of paraphrasing one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.mainai_execution.approval import LIVE_DISPATCHABLE_TASK_STATUSES, requires_approval
from app.models.agent_coordination import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentWorkAssignment,
    CoordinationAgent,
    WorkAssignmentStatus,
)
from app.models.execution_envelope import ExecutionAuthorizationEnvelope, ExecutionScopeProposal
from app.models.mainai_execution import (
    ACTIVE_MAINAI_GOAL_STATUSES,
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.work_candidate import WorkCandidate

# A running job whose lease expired this long ago is reported as abandoned rather than merely
# "reclaimable right now" (the lease itself is 120 s by default). Reading only: the worker's
# reclaim path is the single place that may act on it.
STALE_JOB_GRACE = timedelta(minutes=10)
# A queued job nobody has claimed for this long is a queue problem, not a busy worker.
STUCK_QUEUE_AFTER = timedelta(minutes=15)
# An assignment that has not been touched for this long while still non-terminal is flagged as
# possibly abandoned -- exactly the "identify old or interrupted jobs WITHOUT stopping them"
# requirement, applied to the coordination layer.
STALE_ASSIGNMENT_AFTER = timedelta(hours=6)

_MAX_ITEMS = 5

_TITLE_STOPWORDS = frozenset(
    {"och", "att", "för", "i", "på", "som", "med", "av", "den", "det", "en", "ett", "till", "the", "a", "an", "of", "to"}
)
_DUPLICATE_SIMILARITY = 0.75


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _age_minutes(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds() // 60))


def _normalise_title(title: str) -> set[str]:
    cleaned = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " " for ch in title)
    return {word for word in cleaned.split() if word and word not in _TITLE_STOPWORDS}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _suspected_duplicates(
    *, open_goals: Iterable[MainAIGoal], completed_goals: Iterable[MainAIGoal]
) -> list[dict[str, Any]]:
    """Goals whose title says the same thing as an already-completed goal.

    A SUGGESTION with its evidence, never a block: goal creation stays exactly as authorized
    today. The point is that Life can say "det här ser ut som arbete vi redan gjort" instead of
    silently queuing a second, near-identical goal.
    """
    finished = [(goal, _normalise_title(goal.title)) for goal in completed_goals]
    found: list[dict[str, Any]] = []
    for goal in open_goals:
        words = _normalise_title(goal.title)
        best: tuple[float, MainAIGoal | None] = (0.0, None)
        for other, other_words in finished:
            score = _similarity(words, other_words)
            if score > best[0]:
                best = (score, other)
        if best[1] is not None and best[0] >= _DUPLICATE_SIMILARITY:
            found.append(
                {
                    "goal_id": str(goal.id),
                    "title": goal.title,
                    "matches_completed_goal": {"id": str(best[1].id), "title": best[1].title},
                    "similarity": round(best[0], 2),
                    "note": "möjlig dubblett av redan avslutat arbete — grundaren avgör",
                }
            )
    return found[:_MAX_ITEMS]


def _active_goals(db: Session, owner_id: uuid.UUID) -> tuple[list[MainAIGoal], dict[uuid.UUID, MainAIPlan]]:
    goals = list(
        db.execute(
            select(MainAIGoal)
            .where(MainAIGoal.owner_id == owner_id, MainAIGoal.status.in_(tuple(ACTIVE_MAINAI_GOAL_STATUSES)))
            .order_by(MainAIGoal.created_at.desc())
        )
        .scalars()
        .all()
    )
    plans: dict[uuid.UUID, MainAIPlan] = {}
    if goals:
        for plan in (
            db.execute(
                select(MainAIPlan).where(
                    MainAIPlan.owner_id == owner_id,
                    MainAIPlan.goal_id.in_([g.id for g in goals]),
                    MainAIPlan.status == MainAIPlanStatus.active,
                )
            )
            .scalars()
            .all()
        ):
            plans[plan.goal_id] = plan
    return goals, plans


def _task_summary(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    rows = db.execute(
        select(MainAITask.status, func.count())
        .where(MainAITask.owner_id == owner_id)
        .group_by(MainAITask.status)
    ).all()
    counts = {str(status.value if hasattr(status, "value") else status): int(n) for status, n in rows}

    live_tasks = list(
        db.execute(
            select(MainAITask)
            .where(MainAITask.owner_id == owner_id, MainAITask.status.in_(tuple(LIVE_DISPATCHABLE_TASK_STATUSES)))
            .order_by(MainAITask.created_at.desc())
        )
        .scalars()
        .all()
    )
    goals = (
        {
            goal.id: goal
            for goal in db.execute(
                select(MainAIGoal).where(MainAIGoal.owner_id == owner_id, MainAIGoal.id.in_([t.goal_id for t in live_tasks]))
            )
            .scalars()
            .all()
        }
        if live_tasks
        else {}
    )

    granted: set[uuid.UUID] = set()
    if live_tasks:
        granted = {
            row[0]
            for row in db.execute(
                select(MainAITaskEvent.task_id).where(
                    MainAITaskEvent.owner_id == owner_id,
                    MainAITaskEvent.task_id.in_([t.id for t in live_tasks]),
                    MainAITaskEvent.event_type == MainAITaskEventType.approval_granted,
                )
            ).all()
        }

    awaiting: list[dict[str, Any]] = []
    for task in live_tasks:
        goal = goals.get(task.goal_id)
        if goal is None:
            continue
        # The SAME function the dispatch gate calls -- see app/mainai_execution/approval.py.
        if not requires_approval(
            policy_name=goal.approval_policy,
            task_type=task.task_type,
            task_approval_required=task.approval_required,
        ):
            continue
        if task.id in granted:
            continue
        awaiting.append(
            {
                "task_id": str(task.id),
                "goal_id": str(goal.id),
                "goal_title": goal.title,
                "description": task.description[:280],
                "task_type": task.task_type,
                "status": task.status.value,
                "risk_level": task.risk_level.value,
                "source": f"mainai_tasks:{task.id}",
            }
        )

    blocked = [
        {
            "task_id": str(task.id),
            "goal_id": str(task.goal_id),
            "description": task.description[:200],
            "status": task.status.value,
            "blocker_reason": task.blocker_reason,
            "attempts": task.attempts,
            "source": f"mainai_tasks:{task.id}",
        }
        for task in live_tasks
        if task.status in (MainAITaskStatus.blocked, MainAITaskStatus.retryable_failed)
    ]

    return {
        "counts": counts,
        "awaiting_approval": awaiting[:_MAX_ITEMS],
        "awaiting_approval_count": len(awaiting),
        "blocked": blocked[:_MAX_ITEMS],
    }


def _job_summary(db: Session, owner_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    rows = db.execute(
        select(MainAIJob.status, func.count())
        .where(MainAIJob.owner_id == owner_id)
        .group_by(MainAIJob.status)
    ).all()
    counts = {str(status.value if hasattr(status, "value") else status): int(n) for status, n in rows}

    live = list(
        db.execute(
            select(MainAIJob)
            .where(
                MainAIJob.owner_id == owner_id,
                MainAIJob.status.in_((MainAIJobStatus.queued, MainAIJobStatus.running, MainAIJobStatus.paused)),
            )
            .order_by(MainAIJob.created_at.desc())
        )
        .scalars()
        .all()
    )

    stalled: list[dict[str, Any]] = []
    for job in live:
        reference = job.last_heartbeat_at or job.started_at or job.created_at
        age = _age_minutes(reference, now) or 0
        abandoned = (
            job.status == MainAIJobStatus.running
            and job.lease_expires_at is not None
            and now - job.lease_expires_at > STALE_JOB_GRACE
        )
        stuck = job.status == MainAIJobStatus.queued and now - job.created_at > STUCK_QUEUE_AFTER
        if not (abandoned or stuck):
            continue
        stalled.append(
            {
                "job_id": str(job.id),
                "job_type": job.job_type,
                "status": job.status.value,
                "reason": "lease gick ut utan avslut" if abandoned else "ingen worker har tagit jobbet",
                "age_minutes": age,
                "locked_by": job.locked_by,
                "source": f"mainai_jobs:{job.id}",
            }
        )

    return {
        "counts": counts,
        "active": len(live),
        "stalled": stalled[:_MAX_ITEMS],
        "stalled_count": len(stalled),
        "note": "stilla jobb rapporteras men stoppas aldrig härifrån; workerns lease är enda återtagaren",
    }


def _agent_summary(db: Session, owner_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    assignments = list(
        db.execute(
            select(AgentWorkAssignment)
            .where(AgentWorkAssignment.owner_id == owner_id, AgentWorkAssignment.status.notin_(terminal))
            .order_by(AgentWorkAssignment.updated_at.asc())
        )
        .scalars()
        .all()
    )
    # The agent REGISTRY is deliberately founder-wide system knowledge, not owner-scoped
    # (see app/models/agent_coordination.py's own docstring) — the assignments below are the
    # owner-scoped part, and they are the only rows whose status this module interprets.
    agents = list(db.execute(select(CoordinationAgent)).scalars().all())
    names = {agent.id: agent.display_name for agent in agents}

    blocked = [
        {
            "assignment_id": str(a.id),
            "agent": names.get(a.agent_id),
            "role": a.role.value,
            "status": a.status.value,
            "branch": a.branch,
            "source": f"agent_work_assignments:{a.id}",
        }
        for a in assignments
        if a.status
        in (
            WorkAssignmentStatus.blocked,
            WorkAssignmentStatus.changes_requested,
            WorkAssignmentStatus.waiting_agent,
            WorkAssignmentStatus.waiting_dependency,
        )
    ]
    stale = [
        {
            "assignment_id": str(a.id),
            "agent": names.get(a.agent_id),
            "role": a.role.value,
            "status": a.status.value,
            "idle_minutes": _age_minutes(a.updated_at, now),
            "source": f"agent_work_assignments:{a.id}",
        }
        for a in assignments
        if a.status == WorkAssignmentStatus.running and now - a.updated_at > STALE_ASSIGNMENT_AFTER
    ]

    return {
        "registered": len(agents),
        "available": [agent.display_name for agent in agents if agent.status.value == "active"][:_MAX_ITEMS],
        "open_assignments": len(assignments),
        "by_status": {
            status.value: sum(1 for a in assignments if a.status == status) for status in {a.status for a in assignments}
        },
        "blocked": blocked[:_MAX_ITEMS],
        "stale": stale[:_MAX_ITEMS],
        "note": "WORKING/BLOCKED/DONE kommer från uppdragets egen status, aldrig från en gissning",
    }


def _authorization_summary(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    proposals = list(
        db.execute(
            select(ExecutionScopeProposal)
            .where(ExecutionScopeProposal.owner_id == owner_id, ExecutionScopeProposal.status == "unreviewed")
            .order_by(ExecutionScopeProposal.created_at.desc())
        )
        .scalars()
        .all()
    )
    active_envelopes = db.execute(
        select(func.count())
        .select_from(ExecutionAuthorizationEnvelope)
        .where(
            ExecutionAuthorizationEnvelope.owner_id == owner_id,
            ExecutionAuthorizationEnvelope.status == "active",
        )
    ).scalar_one()
    return {
        "pending_scope_proposals": [
            {
                "proposal_id": str(p.id),
                "goal_id": str(p.goal_id),
                "capabilities": list(p.proposed_capabilities or []),
                "paths": list(p.proposed_paths or []),
                "risk": p.proposed_risk,
                "source": f"execution_scope_proposals:{p.id}",
            }
            for p in proposals[:_MAX_ITEMS]
        ],
        "pending_scope_proposals_count": len(proposals),
        "active_envelopes": int(active_envelopes),
        "note": "föreslagen omfattning är aldrig auktoriserad; endast grundarens befintliga endpoint skapar ett envelope",
    }


def _candidate_summary(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    rows = list(
        db.execute(
            select(WorkCandidate)
            .where(WorkCandidate.owner_id == owner_id, WorkCandidate.status == "unreviewed")
            .order_by(WorkCandidate.created_at.desc())
        )
        .scalars()
        .all()
    )
    return {
        "unreviewed_count": len(rows),
        "unreviewed": [
            {"candidate_id": str(c.id), "title": c.title, "priority": c.priority, "source": f"work_candidates:{c.id}"}
            for c in rows[:_MAX_ITEMS]
        ],
    }


def _work_warnings(*, tasks: dict[str, Any], jobs: dict[str, Any], agents: dict[str, Any]) -> list[str]:
    """Where work is stuck or unknown -- so the founder never reads silence as "all good"."""
    warnings: list[str] = []
    if tasks["awaiting_approval_count"]:
        warnings.append(
            f"{tasks['awaiting_approval_count']} uppgift(er) väntar på grundarens godkännande innan de får köras."
        )
    if tasks["blocked"]:
        warnings.append(
            f"{len(tasks['blocked'])} uppgift(er) är blockerade och kräver ett beslut eller en beroendeåtgärd."
        )
    if jobs["stalled_count"]:
        warnings.append(
            f"{jobs['stalled_count']} jobb ser övergivna ut (lease/queue) — de rapporteras men stoppas inte automatiskt."
        )
    if agents["stale"]:
        warnings.append(f"{len(agents['stale'])} agentuppdrag har stått stilla länge och kan vara avbrutna.")
    if agents["blocked"]:
        warnings.append(f"{len(agents['blocked'])} agentuppdrag väntar på beroende, granskning eller en agent.")
    return warnings


def build_work_chain(db: Session, *, owner_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """What the authorized work actually looks like right now (read-only)."""
    now = now or datetime.utcnow()
    goals, plans = _active_goals(db, owner_id)
    completed = list(
        db.execute(
            select(MainAIGoal)
            .where(MainAIGoal.owner_id == owner_id, MainAIGoal.status == MainAIGoalStatus.completed)
            .order_by(MainAIGoal.completed_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )

    task_counts_by_goal: dict[uuid.UUID, dict[str, int]] = {}
    if goals:
        for goal_id, status, count in db.execute(
            select(MainAITask.goal_id, MainAITask.status, func.count())
            .where(MainAITask.owner_id == owner_id, MainAITask.goal_id.in_([g.id for g in goals]))
            .group_by(MainAITask.goal_id, MainAITask.status)
        ).all():
            task_counts_by_goal.setdefault(goal_id, {})[
                str(status.value if hasattr(status, "value") else status)
            ] = int(count)

    tasks = _task_summary(db, owner_id)
    jobs = _job_summary(db, owner_id, now)
    agents = _agent_summary(db, owner_id, now)

    return {
        "goals": {
            "active_count": len(goals),
            "active": [
                {
                    "goal_id": str(goal.id),
                    "title": goal.title,
                    "status": goal.status.value,
                    "approval_policy": goal.approval_policy,
                    "created_at": _iso(goal.created_at),
                    "plan_version": plans[goal.id].version if goal.id in plans else None,
                    "task_counts": task_counts_by_goal.get(goal.id, {}),
                    "source": f"mainai_goals:{goal.id}",
                }
                for goal in goals[:_MAX_ITEMS]
            ],
            "completed_count": len(completed),
        },
        "tasks": tasks,
        "jobs": jobs,
        "agents": agents,
        "authorizations": _authorization_summary(db, owner_id),
        "candidates": _candidate_summary(db, owner_id),
        "suspected_duplicates": _suspected_duplicates(open_goals=goals, completed_goals=completed),
        "warnings": _work_warnings(tasks=tasks, jobs=jobs, agents=agents),
    }
