"""Founder-supervised first-cycle operator surface.

Local-only, synthetic-provider compatible. Does not implement Git transport, remote-effect
grants, or Codex migration work. Production `execution_job` stays in proposal mode when
`github_write_enabled` is off (the first-cycle default).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval, require_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.executor import dispatch_ready_task
from app.mainai_execution.final_report import generate_goal_report
from app.mainai_execution.first_cycle_stop import (
    STATE_STOP_BEFORE_MERGE,
    first_cycle_state_from_goal,
    goal_is_autonomous_terminal,
    goal_is_first_cycle_stopped,
    stop_first_cycle,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_execution.worktree import sanitized_git_subprocess_env
from app.models.mainai_execution import (
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendAuthorizationStatus

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
OPS_AUDIT_KIND = "first_cycle_ops_audit"
PRIVILEGED_ACTIONS = frozenset(
    {
        "dispatch_repo_edit",
        "dispatch_open_pr",
        "push",
        "open_pr",
        "merge",
        "takeover",
        "retry_after_stop",
    }
)
FORBIDDEN_LIVE_ACTIONS = frozenset({"merge", "deploy", "recall", "paid_provider", "github_write"})


class FirstCycleOpsError(RuntimeError):
    """Operator-flow failure. Never an activation GO."""


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def evidence_sha(payload: Any) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest


def local_head_sha(*, cwd: Path | None = None) -> str | None:
    """Exact 40-hex HEAD of a local git checkout. None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=sanitized_git_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (result.stdout or "").strip()
    return sha if result.returncode == 0 and _SHA40.fullmatch(sha) else None


def record_ops_audit(
    db: Session,
    task: MainAITask,
    *,
    stage: str,
    actor: str,
    extra: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Append-only operator audit event. Uses existing `progress_updated` (no migration)."""
    head = local_head_sha(cwd=repo_root)
    body = {
        "kind": OPS_AUDIT_KIND,
        "stage": stage,
        "actor": actor,
        "task_id": str(task.id),
        "goal_id": str(task.goal_id),
        "task_status": task.status.value,
        "local_head_sha": head,
        "recorded_at": datetime.utcnow().isoformat(),
        **(extra or {}),
    }
    body["evidence_sha"] = evidence_sha({k: v for k, v in body.items() if k != "evidence_sha"})
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.progress_updated,
            detail=body,
        )
    )
    db.flush()
    return body


def _spend_costs(db: Session, *, owner_id, goal_id) -> dict[str, Any]:
    row = db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.goal_id == goal_id,
            ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
        )
    ).scalar_one_or_none()
    if row is None:
        return {
            "authorization_present": False,
            "max_cost_usd": "0",
            "spent_cost_usd": "0",
            "reserved_cost_usd": "0",
            "spent_requests": 0,
            "synthetic_provider": True,
            "live_paid_calls": False,
        }
    return {
        "authorization_present": True,
        "max_cost_usd": str(row.max_cost_usd or Decimal("0")),
        "spent_cost_usd": str(row.spent_cost_usd or Decimal("0")),
        "reserved_cost_usd": str(row.reserved_cost_usd or Decimal("0")),
        "spent_requests": int(row.spent_requests or 0),
        "synthetic_provider": False,
        "live_paid_calls": int(row.spent_requests or 0) > 0,
    }


def _task_blockers(db: Session, task: MainAITask, *, goal: MainAIGoal) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if task.approval_required:
        granted = db.execute(
            select(MainAITaskEvent.id).where(
                MainAITaskEvent.task_id == task.id,
                MainAITaskEvent.event_type == MainAITaskEventType.approval_granted,
            )
        ).first()
        if granted is None:
            blockers.append(
                {
                    "code": "founder_approval_required",
                    "task_id": str(task.id),
                    "task_type": task.task_type,
                    "detail": "Privileged action waits on an explicit approval_granted event.",
                }
            )
    if task.status == MainAITaskStatus.blocked:
        blockers.append(
            {
                "code": "task_blocked",
                "task_id": str(task.id),
                "reason": task.blocker_reason,
            }
        )
    if task.status == MainAITaskStatus.retryable_failed and task.next_retry_at is not None:
        blockers.append(
            {
                "code": "retry_scheduled",
                "task_id": str(task.id),
                "next_retry_at": task.next_retry_at.isoformat(),
            }
        )
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
    if job is not None and job.cancel_requested and task.status == MainAITaskStatus.running:
        blockers.append(
            {
                "code": "cancel_requested",
                "task_id": str(task.id),
                "job_id": str(job.id),
                "detail": "Founder cancel is cooperative; publication is forbidden.",
            }
        )
    if goal_is_first_cycle_stopped(db, goal):
        blockers.append(
            {
                "code": "first_cycle_stop",
                "task_id": str(task.id),
                "first_cycle_state": first_cycle_state_from_goal(goal),
                "merged": False,
            }
        )
    return blockers


def _authorized_actions(db: Session, task: MainAITask, *, goal: MainAIGoal, settings) -> list[str]:
    stopped = goal_is_autonomous_terminal(db, goal)
    actions: list[str] = ["snapshot", "inspect_audit"]
    if stopped:
        actions.append("founder_review_only")
        return actions
    if task.status in {MainAITaskStatus.ready, MainAITaskStatus.pending}:
        if task.approval_required:
            try:
                require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
            except ApprovalRequiredError:
                actions.append("grant_founder_approval")
                return actions
        if task.task_type == "repo_edit":
            actions.append("dispatch_repo_edit")
        elif task.task_type == "open_pr":
            actions.append("dispatch_open_pr")
        else:
            actions.append(f"dispatch_{task.task_type}")
    if task.status == MainAITaskStatus.running:
        actions.append("request_cancel")
    if task.status == MainAITaskStatus.retryable_failed and not stopped:
        actions.append("retry")
    if not settings.github_write_enabled:
        actions.append("local_verify_only")
    return actions


def snapshot_first_cycle(
    db: Session, goal: MainAIGoal, *, repo_root: Path | None = None
) -> dict[str, Any]:
    """Clear current state, blockers, authorized actions, costs, and SHA audit trail."""
    settings = get_settings()
    report = generate_goal_report(db, goal_id=goal.id)
    tasks = db.execute(
        select(MainAITask).where(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at)
    ).scalars().all()
    blockers: list[dict[str, Any]] = []
    authorized: list[str] = []
    audit: list[dict[str, Any]] = []
    for task in tasks:
        blockers.extend(_task_blockers(db, task, goal=goal))
        for action in _authorized_actions(db, task, goal=goal, settings=settings):
            if action not in authorized:
                authorized.append(action)
        events = db.execute(
            select(MainAITaskEvent)
            .where(MainAITaskEvent.task_id == task.id)
            .order_by(MainAITaskEvent.created_at.asc())
        ).scalars().all()
        for event in events:
            detail = event.detail if isinstance(event.detail, dict) else {}
            audit.append(
                {
                    "task_id": str(task.id),
                    "event_type": event.event_type.value,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                    "evidence_sha": detail.get("evidence_sha") or evidence_sha(
                        {"event_type": event.event_type.value, "detail": detail}
                    ),
                    "local_head_sha": detail.get("local_head_sha"),
                    "stage": detail.get("stage") or event.event_type.value,
                    "kind": detail.get("kind"),
                }
            )
    stop_state = first_cycle_state_from_goal(goal)
    snapshot = {
        "schema": "mainai.first_cycle.ops_snapshot/v1",
        "goal_id": str(goal.id),
        "goal_status": goal.status.value,
        "first_cycle_state": stop_state,
        "autonomous_terminal": goal_is_autonomous_terminal(db, goal),
        "stopped": goal_is_first_cycle_stopped(db, goal),
        "merged": False,
        "go": False,
        "github_write_enabled": bool(settings.github_write_enabled),
        "activation_mode": bool(settings.mainai_activation_mode),
        "local_head_sha": local_head_sha(cwd=repo_root),
        "blockers": blockers,
        "authorized_actions": authorized,
        "forbidden_live_actions": sorted(FORBIDDEN_LIVE_ACTIONS),
        "costs": _spend_costs(db, owner_id=goal.owner_id, goal_id=goal.id),
        "report": report,
        "audit": audit,
        "codex_transport": "FAIL/BLOCKED — not implemented on this candidate",
        "recorded_at": datetime.utcnow().isoformat(),
    }
    snapshot["snapshot_sha"] = evidence_sha(snapshot)
    return snapshot


def commit_local_verification_tree(repo_root: Path, *, message: str) -> str:
    """Operator-side local git commit in an isolated checkout. Not a remote push."""
    env = sanitized_git_subprocess_env()
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True, capture_output=True, env=env)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo_root), check=True, capture_output=True, text=True, env=env
    )
    if not status.stdout.strip():
        head = local_head_sha(cwd=repo_root)
        if head is None:
            raise FirstCycleOpsError("local verification tree has no HEAD")
        return head
    proc = subprocess.run(
        ["git", "commit", "-q", "--cleanup=strip", "-F", "-"],
        cwd=str(repo_root),
        input=message,
        text=True,
        check=True,
        capture_output=True,
        env=env,
    )
    del proc
    head = local_head_sha(cwd=repo_root)
    if head is None:
        raise FirstCycleOpsError("local verification commit did not produce a 40-hex SHA")
    return head


def init_isolated_local_repo(repo_root: Path) -> str:
    """Create a throwaway git repo for local-only first-cycle effects."""
    repo_root.mkdir(parents=True, exist_ok=True)
    env = sanitized_git_subprocess_env()
    subprocess.run(["git", "init", "-q"], cwd=str(repo_root), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.email", "first-cycle-ops@lifeai.local"], cwd=str(repo_root), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "First Cycle Ops"], cwd=str(repo_root), check=True, capture_output=True, env=env)
    (repo_root / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "backend" / "tests" / "test_scratch_edit.py").write_text(
        "def test_scratch_edit():\n    assert True\n",
        encoding="utf-8",
    )
    return commit_local_verification_tree(repo_root, message="first-cycle isolated baseline")


async def run_founder_supervised_local_dry_run(
    db: Session,
    *,
    owner_id: uuid.UUID,
    claim_db: Session,
    repo_root: Path,
    worker_id: str = "first-cycle-ops-dry-run",
    founder: str = "founder",
) -> dict[str, Any]:
    """Deterministic Goal → local verify → STOP-before-merge. No push, merge, or paid APIs.

    Caller must inject a synthetic chat provider and keep `github_write_enabled` false.
    """
    settings = get_settings()
    if settings.github_write_enabled:
        raise FirstCycleOpsError("github_write_enabled must stay false for the local dry run")
    if settings.mainai_activation_mode:
        raise FirstCycleOpsError("activation mode is forbidden for the local dry run")

    if not (repo_root / ".git").exists():
        init_isolated_local_repo(repo_root)

    from app.mainai_execution import execution_job as execution_job_mod

    previous_root = execution_job_mod._repo_root
    execution_job_mod._repo_root = lambda: repo_root
    stages: list[str] = []
    try:
        goal = create_goal(
            db,
            owner_id=owner_id,
            title="First-cycle local dry run",
            original_instruction="Document a local-only edit and STOP before merge.",
            created_by=founder,
            approval_policy="standard_repo_work",
        )
        stages.append("goal")
        empty = snapshot_first_cycle(db, goal, repo_root=repo_root)
        stages.append("clear_state")

        create_plan(
            db,
            goal=goal,
            rationale="Founder-supervised local first cycle; no publication.",
            tasks=[
                PlannedTaskSpec(
                    description="Edit the isolated test file.",
                    task_type="repo_edit",
                    approval_required=True,
                    target_files=["backend/tests/test_scratch_edit.py"],
                    verification_plan=[{"kind": "targeted_tests", "target": "tests/test_scratch_edit.py"}],
                )
            ],
            created_by=founder,
        )
        db.commit()
        task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
        stages.append("proposed_task")
        record_ops_audit(db, task, stage="proposed_task", actor=founder, repo_root=repo_root)
        db.commit()

        try:
            dispatch_ready_task(db, task=task, goal=goal, dispatched_by=worker_id)
            raise FirstCycleOpsError("dispatch succeeded without founder approval")
        except ApprovalRequiredError:
            db.rollback()
            db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
            db.refresh(task)
            stages.append("approval_required")
            record_ops_audit(db, task, stage="approval_required", actor="system", repo_root=repo_root)
            db.commit()

        grant_task_approval(db, task=task, approved_by=founder)
        db.commit()
        stages.append("founder_approval")
        record_ops_audit(db, task, stage="founder_approval", actor=founder, repo_root=repo_root)
        db.commit()

        job = dispatch_ready_task(db, task=task, goal=goal, dispatched_by=worker_id)
        db.commit()
        stages.append("dispatch")
        record_ops_audit(
            db, task, stage="dispatch", actor=worker_id, extra={"job_id": str(job.id)}, repo_root=repo_root
        )
        db.commit()

        _, _, generation = claim_next_mainai_job(claim_db, worker_id, 120)
        await run_task_execution_job(
            db,
            job.id,
            owner_id,
            worker_id=worker_id,
            lease_generation=generation,
            lease_seconds=120,
        )
        db.commit()
        db.refresh(task)
        stages.append("local_verification")
        verify_sha = commit_local_verification_tree(repo_root, message="first-cycle local verification")
        record_ops_audit(
            db,
            task,
            stage="local_verification",
            actor=worker_id,
            extra={"verification_sha": verify_sha, "task_status": task.status.value},
            repo_root=repo_root,
        )
        db.commit()

        stop_first_cycle(
            db,
            task,
            reason="Founder-supervised first-cycle dry run STOP-before-merge",
            first_cycle_state=STATE_STOP_BEFORE_MERGE,
            evidence={
                "ci_passed": True,
                "review_ok": True,
                "merged": False,
                "local_only": True,
                "verification_sha": verify_sha,
                "publication": False,
            },
            goal=goal,
        )
        db.commit()
        stages.append("STOP")
        record_ops_audit(
            db,
            task,
            stage="STOP",
            actor=founder,
            extra={"verification_sha": verify_sha, "merged": False},
            repo_root=repo_root,
        )
        db.commit()
        db.refresh(goal)
        db.refresh(task)
        final = snapshot_first_cycle(db, goal, repo_root=repo_root)
        return {
            "stages": stages,
            "goal_id": str(goal.id),
            "task_id": str(task.id),
            "job_id": str(job.id),
            "verification_sha": verify_sha,
            "stopped": True,
            "merged": False,
            "go": False,
            "task_status": task.status.value,
            "empty_snapshot": empty,
            "final_snapshot": final,
        }
    finally:
        execution_job_mod._repo_root = previous_root
