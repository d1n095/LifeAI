"""Founder-ops snapshot: durable rows only, no fabricated operational narrative."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs.heartbeat import worker_process_alive
from app.mainai_founder_ops.status import (
    classify_agent_task,
    classify_mainai_job,
    classify_mainai_task,
    classify_workforce_assignment,
)
from app.mainai_startup_readiness import evaluate_startup_readiness
from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.mainai_execution import MainAICheckpoint, MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskEvent
from app.models.mainai_job import MainAIJob
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendAuthorizationStatus
from app.models.usage import UsageLog
from app.models.work_candidate import WorkCandidate
from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest
from app.workforce.kill_switch import query_stop_status


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.isoformat()


def _kill_switch_status(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    """Fail closed when STOP cannot be read: unknown is treated as blocking, never as inactive."""
    try:
        status = query_stop_status(db, owner_id=owner_id)
        status["observability_failed"] = False
        return status
    except Exception as exc:  # noqa: BLE001 — founder UI must not under-report STOP
        return {
            "blocked": True,
            "code": "STOP_STATUS_UNKNOWN",
            "observability_failed": True,
            "error": type(exc).__name__,
            "canonical_table": "workforce_authority_epoch",
            "global": {"active": None, "reason": None, "sequence": None},
            "owner": {"active": None, "reason": None, "sequence": None, "owner_id": str(owner_id)},
        }


def _startup_readiness(db: Session) -> dict[str, Any]:
    try:
        report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=db)
        return report.as_dict()
    except Exception as exc:  # noqa: BLE001
        return {
            "level": "UNKNOWN",
            "blocking": ["startup_readiness_unavailable"],
            "error": type(exc).__name__,
            "unknown_fail_closed": True,
            "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN",
        }


def _live_spend(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    usage = db.execute(
        select(
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
            func.count(UsageLog.id).filter(UsageLog.cost_usd.is_(None)),
        ).where(UsageLog.user_id == owner_id)
    ).one()
    request_count, cost_sum, unknown_count = int(usage[0] or 0), usage[1], int(usage[2] or 0)
    grants = list(
        db.execute(
            select(ProviderSpendAuthorization)
            .where(
                ProviderSpendAuthorization.owner_id == owner_id,
                ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
            )
            .order_by(ProviderSpendAuthorization.created_at.desc())
            .limit(20)
        ).scalars()
    )
    grant_spent = sum((g.spent_cost_usd or Decimal("0")) for g in grants)
    grant_reserved = sum((g.reserved_cost_usd or Decimal("0")) for g in grants)
    usage_cost: float | None
    if unknown_count > 0:
        usage_cost = None
    else:
        usage_cost = float(cost_sum or 0)
    return {
        "spend_live": True,
        "source": "usage_log_and_provider_spend_authorizations",
        "usage_request_count": request_count,
        "usage_cost_usd": usage_cost,
        "usage_cost_unknown_count": unknown_count,
        "active_grant_count": len(grants),
        "active_grant_spent_usd": float(grant_spent),
        "active_grant_reserved_usd": float(grant_reserved),
        "grants": [
            {
                "id": str(g.id),
                "goal_id": str(g.goal_id),
                "status": g.status,
                "max_cost_usd": float(g.max_cost_usd),
                "spent_cost_usd": float(g.spent_cost_usd or 0),
                "reserved_cost_usd": float(g.reserved_cost_usd or 0),
                "spent_requests": g.spent_requests,
                "max_requests": g.max_requests,
            }
            for g in grants
        ],
    }


def _conversation_count_today(db: Session, owner_id: uuid.UUID) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.execute(
            select(func.count(Conversation.id)).where(
                Conversation.user_id == owner_id,
                Conversation.updated_at >= start,
            )
        ).scalar_one()
    )


def _latest_checkpoint_for_task(db: Session, task_id: uuid.UUID) -> MainAICheckpoint | None:
    return db.execute(
        select(MainAICheckpoint)
        .where(MainAICheckpoint.task_id == task_id)
        .order_by(MainAICheckpoint.created_at.desc())
        .limit(1)
    ).scalars().first()


def _task_events(db: Session, task_id: uuid.UUID) -> list[MainAITaskEvent]:
    return list(
        db.execute(
            select(MainAITaskEvent)
            .where(MainAITaskEvent.task_id == task_id)
            .order_by(MainAITaskEvent.created_at.asc())
        ).scalars()
    )


def _jobs_by_id(db: Session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, MainAIJob]:
    if not job_ids:
        return {}
    rows = db.execute(select(MainAIJob).where(MainAIJob.id.in_(job_ids))).scalars()
    return {row.id: row for row in rows}


def _safe_launch_flags(*, recall_http_mounted: bool) -> dict[str, Any]:
    settings = get_settings()
    return {
        "github_write_enabled": bool(settings.github_write_enabled),
        "github_auto_merge_enabled": bool(settings.github_auto_merge_enabled),
        "mainai_activation_mode": bool(settings.mainai_activation_mode),
        "legacy_prepare_pr_writes_enabled": bool(settings.mainai_legacy_prepare_pr_writes_enabled),
        "recall_http_mounted": bool(recall_http_mounted),
        "privileged_remote_effects_enabled": bool(
            settings.github_write_enabled or settings.github_auto_merge_enabled
        ),
        "note": "Flags are settings/wiring, not a launch GO. First-cycle GO stays false.",
    }


def build_founder_ops_snapshot(
    db: Session,
    *,
    owner_id: uuid.UUID,
    health: dict[str, Any] | None = None,
    recall_http_mounted: bool = False,
) -> dict[str, Any]:
    """Single founder-readable ops snapshot. Evidence only; never one boolean 'ready'."""
    stop = _kill_switch_status(db, owner_id)
    readiness = _startup_readiness(db)
    spend = _live_spend(db, owner_id)
    health = health or {"status": "unknown", "worker": "unknown"}

    goals = list(
        db.execute(
            select(MainAIGoal)
            .where(MainAIGoal.owner_id == owner_id)
            .order_by(MainAIGoal.created_at.desc())
            .limit(20)
        ).scalars()
    )
    tasks = list(
        db.execute(
            select(MainAITask)
            .where(MainAITask.owner_id == owner_id)
            .order_by(MainAITask.created_at.desc())
            .limit(50)
        ).scalars()
    )
    jobs = list(
        db.execute(
            select(MainAIJob)
            .where(MainAIJob.owner_id == owner_id)
            .order_by(MainAIJob.created_at.desc())
            .limit(30)
        ).scalars()
    )
    assignments = list(
        db.execute(
            select(WorkforceAssignment)
            .where(WorkforceAssignment.owner_id == owner_id)
            .order_by(WorkforceAssignment.created_at.desc())
            .limit(30)
        ).scalars()
    )
    agent_tasks = list(
        db.execute(select(AgentTask).order_by(AgentTask.created_at.desc()).limit(20)).scalars()
    )
    candidates = list(
        db.execute(
            select(WorkCandidate)
            .where(WorkCandidate.owner_id == owner_id, WorkCandidate.status == "unreviewed")
            .order_by(WorkCandidate.created_at.desc())
            .limit(15)
        ).scalars()
    )
    open_requests = list(
        db.execute(
            select(WorkforceDelegationRequest)
            .where(
                WorkforceDelegationRequest.owner_id == owner_id,
                WorkforceDelegationRequest.status.in_(("open", "resolving", "assigned")),
            )
            .order_by(WorkforceDelegationRequest.created_at.desc())
            .limit(15)
        ).scalars()
    )

    job_map = _jobs_by_id(db, [t.mainai_job_id for t in tasks if t.mainai_job_id])
    work_items: list[dict[str, Any]] = []
    for task in tasks:
        checkpoint = _latest_checkpoint_for_task(db, task.id)
        work_items.append(
            classify_mainai_task(
                task,
                job_map.get(task.mainai_job_id) if task.mainai_job_id else None,
                events=_task_events(db, task.id),
                checkpoint_test_status=checkpoint.test_status if checkpoint else None,
                checkpoint_ci_status=checkpoint.ci_status if checkpoint else None,
            )
        )
    work_items.extend(classify_mainai_job(job) for job in jobs)
    work_items.extend(classify_workforce_assignment(a) for a in assignments)
    work_items.extend(classify_agent_task(t) for t in agent_tasks)

    blockers: list[dict[str, Any]] = []
    if stop.get("blocked"):
        blockers.append({"kind": "stop", "code": stop.get("code"), "detail": stop})
    for item in work_items:
        if item.get("blocker_reason"):
            blockers.append(
                {
                    "kind": "task_blocker",
                    "task_id": item.get("task_id"),
                    "detail": item["blocker_reason"],
                }
            )
        if item.get("founder_status") == "failed":
            blockers.append(
                {
                    "kind": "failed_work",
                    "source": item.get("source"),
                    "id": item.get("task_id") or item.get("job_id") or item.get("assignment_id"),
                    "source_status": item.get("source_status"),
                    "failure_class": item.get("failure_class") or item.get("error_category"),
                }
            )
    for goal in goals:
        if goal.status == MainAIGoalStatus.blocked:
            blockers.append(
                {
                    "kind": "goal_blocked",
                    "goal_id": str(goal.id),
                    "title": goal.title,
                    "final_outcome": goal.final_outcome,
                }
            )

    authorization_requests = [
        {
            "id": str(c.id),
            "title": c.title,
            "status": c.status,
            "authorized": False,
            "source": "work_candidate",
        }
        for c in candidates
    ]
    authorization_requests.extend(
        {
            "id": str(t.id),
            "title": t.description,
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "authorized": False,
            "source": "mainai_task_approval",
            "approval_required": True,
        }
        for t in tasks
        if t.approval_required
    )
    # Only keep those that have not actually been approved — approval_granted lives on the
    # task detail join, not the row. Surface every approval_required task; the UI must not
    # invent "already approved".
    authorization_requests.extend(
        {
            "id": str(r.id),
            "title": r.goal_text,
            "status": r.status,
            "authorized": False,
            "source": "workforce_delegation_request",
            "capability": r.required_capability,
        }
        for r in open_requests
    )

    review_results = [
        item
        for item in work_items
        if item.get("source") == "agent_task"
        and item.get("source_status")
        in {"reviewed_approved", "reviewed_needs_correction", "reviewed_rejected"}
    ]
    review_results.extend(
        item for item in work_items if item.get("founder_status") == "independently_verified"
    )

    counts: dict[str, int] = {}
    for item in work_items:
        key = item["founder_status"]
        counts[key] = counts.get(key, 0) + 1

    worker = "alive" if worker_process_alive() is True else (health.get("worker") or "unknown")
    backend_reachable = health.get("status") == "ok"

    first_cycle = []
    try:
        from app.mainai_execution.first_cycle_ops import founder_diagnostics

        first_cycle = [founder_diagnostics(db, goal) for goal in goals[:5]]
    except Exception:  # noqa: BLE001 — diagnostics are supplementary
        first_cycle = []

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_basis": "durable_rows_only",
        "chain_of_thought_exposed": False,
        "fabricated_progress": False,
        "health": {**health, "worker": worker, "backend_reachable": backend_reachable},
        "startup_readiness": readiness,
        "stop": stop,
        "spend": spend,
        "safe_launch": _safe_launch_flags(recall_http_mounted=recall_http_mounted),
        "conversation_count_today": _conversation_count_today(db, owner_id),
        "goals": [
            {
                "id": str(g.id),
                "title": g.title,
                "status": g.status.value if hasattr(g.status, "value") else str(g.status),
                "final_outcome": g.final_outcome,
                "started_at": _iso(g.started_at),
                "completed_at": _iso(g.completed_at),
            }
            for g in goals
        ],
        "work_items": work_items,
        "work_item_counts": counts,
        "blockers": blockers,
        "authorization_requests": authorization_requests,
        "review_results": review_results,
        "first_cycle_go": False,
        "first_cycle": first_cycle,
        "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN",
    }


def recall_http_mounted(app: Any) -> bool:
    return any("/personal-recall" in getattr(route, "path", "") for route in getattr(app, "routes", []))
