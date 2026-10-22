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
    is_independent_exact_sha_receipt,
)
from app.mainai_founder_ops.workflow import (
    build_next_actions,
    dependency_records,
    detect_dependency_cycles,
    independent_ready_work,
    load_dependency_edges,
    pending_approval_tasks,
    task_ids_in_cycles,
)
from app.mainai_startup_readiness import evaluate_startup_readiness
from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.mainai_execution import MainAICheckpoint, MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskEvent
from app.models.mainai_job import MainAIJob
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendAuthorizationStatus
from app.models.usage import UsageLog
from app.models.mainai_verification import MainAIVerificationRecord
from app.models.work_candidate import WorkCandidate
from app.models.workforce import WorkforceAgentProfile, WorkforceAssignment, WorkforceDelegationRequest
from app.workforce.kill_switch import query_stop_status

_ACTIVE_PROFILE_STATUSES = frozenset({"active", "probation", "candidate"})
_LIVE_ASSIGNMENT_STATUSES = frozenset({"assigned", "acknowledged", "running", "awaiting_verification"})


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


def _normalize_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    """BLOCKED without a named blocker is not a legal report (D5)."""
    level = str(payload.get("level") or "")
    blocking = list(payload.get("blocking") or [])
    if level.upper() == "BLOCKED" and not blocking:
        payload = {**payload, "blocking": ["blocked_without_named_cause"]}
    return payload


def _startup_readiness(db: Session) -> dict[str, Any]:
    try:
        report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=db)
        return _normalize_readiness(report.as_dict())
    except Exception as exc:  # noqa: BLE001
        return {
            "level": "UNKNOWN",
            "blocking": ["startup_readiness_unavailable"],
            "error": type(exc).__name__,
            "unknown_fail_closed": True,
            "evidence_unavailable": True,
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


def _task_commit_sha(
    task: MainAITask,
    *,
    checkpoint: MainAICheckpoint | None,
    events: list[MainAITaskEvent],
) -> str | None:
    """Exact artifact SHA this task actually produced. Receipts for any other SHA do not verify it."""
    candidates: list[Any] = []
    if checkpoint is not None and isinstance(checkpoint.executor_state, dict):
        state = checkpoint.executor_state
        work = state.get("work_result") if isinstance(state.get("work_result"), dict) else {}
        finalize = state.get("finalize_info") if isinstance(state.get("finalize_info"), dict) else {}
        candidates.extend(
            [
                work.get("commit_sha"),
                work.get("head_sha"),
                work.get("remote_sha"),
                finalize.get("commit_sha"),
                finalize.get("head_sha"),
                state.get("commit_sha"),
            ]
        )
    for event in reversed(events):
        detail = event.detail if isinstance(event.detail, dict) else {}
        work = detail.get("work_result") if isinstance(detail.get("work_result"), dict) else {}
        candidates.extend(
            [
                work.get("commit_sha"),
                work.get("head_sha"),
                detail.get("commit_sha"),
                detail.get("head_sha"),
                detail.get("candidate_sha"),
            ]
        )
    for sha in candidates:
        if isinstance(sha, str) and len(sha) == 40 and sha.islower() and all(ch in "0123456789abcdef" for ch in sha):
            return sha
    return None


def _receipt_matches_task(row: MainAIVerificationRecord, task: MainAITask) -> bool:
    scope = row.verification_scope if isinstance(row.verification_scope, dict) else {}
    keys = {
        row.component_id,
        row.candidate_id,
        str(scope.get("task_id") or ""),
        f"mainai_task:{scope.get('task_id')}" if scope.get("task_id") else "",
    }
    return str(task.id) in keys or f"mainai_task:{task.id}" in keys


def _independent_receipts_by_task(
    db: Session,
    tasks: list[MainAITask],
    *,
    expected_sha_by_task: dict[uuid.UUID, str] | None = None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """Independent PASS receipts bound to each task's actual commit. Later FAIL revokes."""
    if not tasks:
        return {}
    rows = list(
        db.execute(select(MainAIVerificationRecord).order_by(MainAIVerificationRecord.reviewed_at.asc(), MainAIVerificationRecord.created_at.asc())).scalars()
    )
    invalidated: set[uuid.UUID] = {
        row.invalidates_verification_id
        for row in rows
        if row.invalidates_verification_id is not None
    }
    expected_sha_by_task = expected_sha_by_task or {}
    found: dict[uuid.UUID, dict[str, Any]] = {}
    for task in tasks:
        expected = expected_sha_by_task.get(task.id)
        if not expected:
            continue
        latest_pass: MainAIVerificationRecord | None = None
        revoked = False
        for row in rows:
            if not _receipt_matches_task(row, task):
                continue
            if row.candidate_sha != expected:
                continue
            result = (row.review_result or "").upper()
            if result == "PASS" and row.examiner_identity != row.builder_identity and row.id not in invalidated:
                latest_pass = row
                revoked = False
            elif result in {"FAIL", "BLOCKED"} and latest_pass is not None:
                revoked = True
                latest_pass = None
            elif row.invalidates_verification_id and latest_pass is not None and row.invalidates_verification_id == latest_pass.id:
                revoked = True
                latest_pass = None
        if latest_pass is None:
            if revoked:
                found[task.id] = {
                    "candidate_sha": expected,
                    "expected_sha": expected,
                    "task_commit_sha": expected,
                    "review_result": "FAIL",
                    "revoked": True,
                }
            continue
        receipt = {
            "candidate_sha": latest_pass.candidate_sha,
            "expected_sha": expected,
            "task_commit_sha": expected,
            "builder_identity": latest_pass.builder_identity,
            "examiner_identity": latest_pass.examiner_identity,
            "review_result": latest_pass.review_result,
            "verification_id": str(latest_pass.id),
            "revoked": False,
        }
        if is_independent_exact_sha_receipt(receipt, expected_sha=expected):
            found[task.id] = receipt
    return found


def _founder_authority(*, recall_http_mounted: bool) -> dict[str, Any]:
    """Explainable restrictions from existing settings — not a new authority subsystem."""
    settings = get_settings()
    restrictions: list[dict[str, str]] = []
    if not settings.github_write_enabled:
        restrictions.append(
            {
                "code": "github_write_disabled",
                "explain": "GitHub-skrivning är avstängd tills grundaren godkänner den här profilen.",
            }
        )
    if not settings.github_auto_merge_enabled:
        restrictions.append(
            {
                "code": "auto_merge_disabled",
                "explain": "Automatisk merge är avstängd. Merge kräver separat Founder-beslut.",
            }
        )
    if not settings.mainai_activation_mode:
        restrictions.append(
            {
                "code": "activation_mode_off",
                "explain": "Aktiveringsläge är av. First-cycle GO förblir false.",
            }
        )
    if not settings.mainai_legacy_prepare_pr_writes_enabled:
        restrictions.append(
            {
                "code": "legacy_prepare_pr_writes_disabled",
                "explain": "Legacy Contents-API PR-skrivning är avstängd.",
            }
        )
    if not getattr(settings, "mainai_allow_live_checkout_writes", False):
        restrictions.append(
            {
                "code": "live_checkout_writes_disabled",
                "explain": "Kodagenten får inte ändra den körande serverns checkout utan explicit Founder-auktorisation.",
            }
        )
    if not recall_http_mounted:
        restrictions.append(
            {
                "code": "recall_http_unmounted",
                "explain": "Personal Recall HTTP är inte monterad och inte aktiverad.",
            }
        )
    return {
        "policy_transparent": True,
        "sensitive_changes_require_founder_approval": True,
        "independent_security_enforcement": True,
        "operational_status_source": "durable_rows_only",
        "first_cycle_go": False,
        "live_checkout_writes_authorized": bool(getattr(settings, "mainai_allow_live_checkout_writes", False)),
        "github_write_enabled": bool(settings.github_write_enabled),
        "recall_http_mounted": bool(recall_http_mounted),
        "restrictions": restrictions,
    }


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


def _active_agents(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignments: list[WorkforceAssignment],
) -> list[dict[str, Any]]:
    """Workforce profiles that exist. Current work comes from live assignments only."""
    profiles = list(
        db.execute(
            select(WorkforceAgentProfile)
            .where(
                WorkforceAgentProfile.owner_id == owner_id,
                WorkforceAgentProfile.retired_at.is_(None),
                WorkforceAgentProfile.status.in_(tuple(_ACTIVE_PROFILE_STATUSES)),
            )
            .order_by(WorkforceAgentProfile.created_at.desc())
            .limit(40)
        ).scalars()
    )
    live_by_profile: dict[uuid.UUID, WorkforceAssignment] = {}
    for assignment in assignments:
        if assignment.revoked_at is not None:
            continue
        if assignment.status not in _LIVE_ASSIGNMENT_STATUSES:
            continue
        live_by_profile.setdefault(assignment.profile_id, assignment)

    agents: list[dict[str, Any]] = []
    for profile in profiles:
        live = live_by_profile.get(profile.id)
        classified = classify_workforce_assignment(live) if live is not None else None
        agents.append(
            {
                "id": str(profile.id),
                "agent_key": profile.agent_key,
                "name": profile.name,
                "role": profile.role,
                "status": profile.status,
                "trust_zone": profile.trust_zone,
                "current_assignment_id": str(live.id) if live is not None else None,
                "current_assignment_status": live.status if live is not None else None,
                "founder_status": classified["founder_status"] if classified else "idle",
                "running": classified["founder_status"] == "running" if classified else False,
            }
        )
    return agents


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
    health = dict(health or {"status": "unknown", "worker": "unknown"})
    if "status" not in health or health.get("status") in {None, ""}:
        health["status"] = "unknown"

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
    events_by_task = {task.id: _task_events(db, task.id) for task in tasks}
    tasks_by_id = {task.id: task for task in tasks}
    checkpoints_by_task = {task.id: _latest_checkpoint_for_task(db, task.id) for task in tasks}
    expected_sha_by_task = {
        task.id: sha
        for task in tasks
        if (sha := _task_commit_sha(task, checkpoint=checkpoints_by_task[task.id], events=events_by_task[task.id]))
    }
    receipts = _independent_receipts_by_task(db, tasks, expected_sha_by_task=expected_sha_by_task)
    linked_job_ids = {task.mainai_job_id for task in tasks if task.mainai_job_id}

    work_items: list[dict[str, Any]] = []
    for task in tasks:
        checkpoint = checkpoints_by_task[task.id]
        work_items.append(
            classify_mainai_task(
                task,
                job_map.get(task.mainai_job_id) if task.mainai_job_id else None,
                events=events_by_task[task.id],
                checkpoint_test_status=checkpoint.test_status if checkpoint else None,
                checkpoint_ci_status=checkpoint.ci_status if checkpoint else None,
                independent_receipt=receipts.get(task.id),
            )
        )
    # D5: a task and its leased job are the same work — do not count both.
    work_items.extend(classify_mainai_job(job) for job in jobs if job.id not in linked_job_ids)
    work_items.extend(classify_workforce_assignment(a) for a in assignments)
    work_items.extend(classify_agent_task(t) for t in agent_tasks)

    cycles = detect_dependency_cycles(load_dependency_edges(db, owner_id=owner_id))
    cyclic_ids = task_ids_in_cycles(cycles)
    independent = independent_ready_work(tasks)
    approvals_pending = pending_approval_tasks(tasks, events_by_task)
    dependencies = dependency_records(db, owner_id=owner_id, tasks_by_id=tasks_by_id)

    blockers: list[dict[str, Any]] = []
    seen_blockers: set[tuple[Any, ...]] = set()

    def _add_blocker(blocker: dict[str, Any]) -> None:
        key = (
            blocker.get("kind"),
            blocker.get("task_id") or blocker.get("id") or blocker.get("goal_id"),
            tuple(blocker.get("task_ids") or ()),
            blocker.get("detail") if blocker.get("kind") == "task_blocker" else None,
        )
        if key in seen_blockers:
            return
        seen_blockers.add(key)
        blockers.append(blocker)

    if stop.get("blocked"):
        _add_blocker({"kind": "stop", "code": stop.get("code"), "detail": stop})
    for cycle in cycles:
        _add_blocker(
            {
                "kind": "circular_dependency",
                "task_ids": cycle,
                "detail": "Stored dependency cycle; no task in the cycle can become ready.",
            }
        )
    for item in work_items:
        if item.get("blocker_reason"):
            _add_blocker(
                {
                    "kind": "task_blocker",
                    "task_id": item.get("task_id"),
                    "detail": item["blocker_reason"],
                }
            )
        if item.get("founder_status") == "failed" and item.get("source_status") != "cancelled" and not item.get("cancelled"):
            _add_blocker(
                {
                    "kind": "failed_work",
                    "source": item.get("source"),
                    "id": item.get("task_id") or item.get("job_id") or item.get("assignment_id"),
                    "source_status": item.get("source_status"),
                    "failure_class": item.get("failure_class") or item.get("error_category"),
                }
            )
        if item.get("task_id") in cyclic_ids:
            _add_blocker(
                {
                    "kind": "task_in_cycle",
                    "task_id": item.get("task_id"),
                    "description": item.get("description"),
                }
            )
    for goal in goals:
        if goal.status == MainAIGoalStatus.blocked:
            _add_blocker(
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
    for task in approvals_pending:
        authorization_requests.append(
            {
                "id": str(task.id),
                "title": task.description,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "authorized": False,
                "source": "mainai_task_approval",
                "approval_required": True,
            }
        )
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

    failed_items = [item for item in work_items if item.get("founder_status") == "failed"]
    next_actions = build_next_actions(
        stop=stop,
        cycles=cycles,
        pending_approvals=approvals_pending,
        independent_ready=independent,
        failed_items=failed_items,
        authorization_open=len(authorization_requests),
    )
    agents = _active_agents(db, owner_id=owner_id, assignments=assignments)

    counts: dict[str, int] = {}
    for item in work_items:
        key = item["founder_status"]
        counts[key] = counts.get(key, 0) + 1

    worker = "alive" if worker_process_alive() is True else (health.get("worker") or "unknown")
    health_status = str(health.get("status") or "unknown")
    health_evidence_available = health_status in {"ok", "unavailable", "error", "degraded"}
    backend_reachable = health_evidence_available and health_status == "ok"

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
        "health": {
            **health,
            "status": health_status,
            "worker": worker,
            "backend_reachable": backend_reachable,
            "evidence_available": health_evidence_available,
        },
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
        "agents": agents,
        "dependencies": dependencies,
        "circular_dependencies": cycles,
        "independent_ready_work": independent,
        "next_actions": next_actions,
        "work_items": work_items,
        "work_item_counts": counts,
        "blocker_counts": {
            "operational": len(blockers),
            "startup_readiness": len(readiness.get("blocking") or []) if isinstance(readiness, dict) else 0,
            "stop": 1 if stop.get("blocked") else 0,
        },
        "blockers": blockers,
        "authorization_requests": authorization_requests,
        "review_results": review_results,
        "first_cycle_go": False,
        "first_cycle": first_cycle,
        "authority": _founder_authority(recall_http_mounted=recall_http_mounted),
        "security": _security_summary(db, owner_id=owner_id, recall_http_mounted=recall_http_mounted),
        "durable_coordination": _durable_coordination(db, owner_id=owner_id),
        "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN",
        "communication_policy": "factual_accuracy_over_agreement",
    }


def _durable_coordination(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """CO-4b/CO-5 visibility. Coordination maturity only — never a first-cycle GO input."""
    try:
        from app.agent_coordination.durable import founder_coordination_snapshot

        view = founder_coordination_snapshot(db, owner_id=owner_id)
        view["available"] = True
        return view
    except Exception as exc:  # noqa: BLE001 — diagnostics must not hide Founder-ops evidence
        return {
            "available": False,
            "capability": "CO-4b/CO-5",
            "first_cycle_go": False,
            "blocks_first_cycle": False,
            "error": type(exc).__name__,
            "obligations": [],
            "active": [],
            "finished": [],
            "independently_certified": [],
            "blocked": [],
            "stalled": [],
            "handoffs": [],
            "owners": {},
            "queue_empty": True,
            "needed_work": {"items": [], "idle_agents": [], "founder_must_forward_reports": False},
            "github_intake": {
                "access": "unknown",
                "candidates": {},
                "stale_rejected": [],
                "delivery_claimed": False,
                "first_cycle_go": False,
            },
            "lifecycle": {
                "delivery_claimed": False,
                "first_cycle_go": False,
                "builder_eligible": [],
                "decisions": [],
                "founder_approval_required": [],
                "github_green_is_independent_certification": False,
            },
        }


def _security_summary(db: Session, *, owner_id: uuid.UUID, recall_http_mounted: bool) -> dict[str, Any]:
    """Supplementary diagnostics. Never a first-cycle blocker or fabricated GO input."""
    try:
        from app.mainai_security.dashboard import security_summary_for_ops

        return security_summary_for_ops(db, owner_id=owner_id, recall_http_mounted=recall_http_mounted)
    except Exception:  # noqa: BLE001 — diagnostics must not hide Founder-ops evidence
        return {
            "available": False,
            "finding_count": 0,
            "confirmed_incident_count": 0,
            "first_cycle_go": False,
            "blocks_first_cycle": False,
            "dashboard_path": "/admin/security",
        }


def recall_http_mounted(app: Any) -> bool:
    return any("/personal-recall" in getattr(route, "path", "") for route in getattr(app, "routes", []))
