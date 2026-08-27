"""Governed repository capabilities below MainAI orchestration.

This module is deliberately not a scheduler and exposes no generic shell primitive. Every
operation is a structured, bounded capability tied to an existing task/job/lease and, for
writes, a structurally verified worktree class:

A) PER-JOB recovery worktree — `MainAITaskWorktree` + `.mainai_worktree_owner.json`
   (`worktree_id` set; see `app/mainai_execution/worktree.py`).

B) PER-GOAL Supervisor production worktree — canonical path/branch from
   `app/development_supervisor/production_worktree.py`, verified against the *active*
   `supervisor_goal_leases` claim (`supervisor_goal_id` + lease id/generation). No
   per-job ownership marker; no `MainAITaskWorktree` row. Callers cannot self-authorize
   with a boolean flag — Operator re-verifies lease + path + branch + task↔goal link.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.mainai_execution.checkpoint import (
    latest_checkpoint_for_step,
    record_checkpoint,
)
from app.mainai_execution.worktree import (
    WorktreeOwnershipError,
    fetch_remote_branch,
    push_worktree_branch,
    verify_worktree_ownership,
)
from app.models.intelligence_governance import IntelligenceExecution
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
from app.models.supervisor_lease import SupervisorGoalLease
from app.models.work_intelligence import WorkStrategyExecution, WorkTraceEvent
from app.work_intelligence import record_trace_event


class OperatorError(RuntimeError):
    pass


class OperatorAuthorizationError(OperatorError):
    pass


class OperatorPathError(OperatorError):
    pass


class OperatorCommandError(OperatorError):
    pass


class OperatorVerificationRequired(OperatorError):
    pass


class OperatorCapabilityMissing(OperatorError):
    def __init__(
        self, capability: str, *, why_needed: str, unrelated_work_can_continue: bool
    ):
        self.capability = capability
        self.why_needed = why_needed
        self.unrelated_work_can_continue = unrelated_work_can_continue
        super().__init__(f"CAPABILITY_MISSING: {capability}: {why_needed}")


READ_ONLY = "READ_ONLY"
LOCAL_WRITE = "LOCAL_WRITE"
LOCAL_EXECUTION = "LOCAL_EXECUTION"
REMOTE_WRITE = "REMOTE_WRITE"

DEVELOPMENT_CAPABILITIES = {
    "repo_status": READ_ONLY,
    "repo_branch": READ_ONLY,
    "repo_head": READ_ONLY,
    "repo_merge_base": READ_ONLY,
    "repo_diff": READ_ONLY,
    "repo_log": READ_ONLY,
    "list_files": READ_ONLY,
    "search_text": READ_ONLY,
    "read_file": READ_ONLY,
    "inspect_git_history": READ_ONLY,
    "create_file": LOCAL_WRITE,
    "patch_file": LOCAL_WRITE,
    "delete_file": LOCAL_WRITE,
    "run_focused_test": LOCAL_EXECUTION,
    "run_static_check": LOCAL_EXECUTION,
    "run_migration_check": LOCAL_EXECUTION,
    "run_full_suite": LOCAL_EXECUTION,
    "stage_scoped_changes": LOCAL_WRITE,
    "commit_scoped_changes": LOCAL_WRITE,
    "push_branch": REMOTE_WRITE,
}

SENSITIVE_PARTS = {
    ".env",
    ".ssh",
    ".aws",
    ".gnupg",
    "credentials",
    "secrets",
    "id_rsa",
    "id_ed25519",
}
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|cookie)\s*[:=]\s*([^\s,;]+)"
)
STANDALONE_SECRET_PATTERN = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})"
)
SAFE_ENV_NAMES = frozenset({"PATH", "TZ", "PGTZ", "PYTHONPATH", "LANG", "LC_ALL"})


@dataclass(frozen=True)
class CommandProfile:
    capability: str
    argv_prefix: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int


COMMAND_PROFILES = {
    "focused_pytest": CommandProfile(
        "run_focused_test", (sys.executable, "-m", "pytest", "-q"), 300, 64_000
    ),
    "full_backend_pytest": CommandProfile(
        "run_full_suite", (sys.executable, "-m", "pytest", "-q"), 1800, 128_000
    ),
    "ruff_check": CommandProfile("run_static_check", ("ruff", "check"), 300, 64_000),
    "git_diff_check": CommandProfile(
        "run_static_check", ("git", "diff", "--check"), 60, 32_000
    ),
    "alembic": CommandProfile("run_migration_check", ("alembic",), 600, 64_000),
}


@dataclass(frozen=True)
class OperatorContext:
    owner_id: uuid.UUID
    task_id: uuid.UUID
    job_id: uuid.UUID
    worker_id: str
    lease_generation: int
    repository_root: Path
    expected_base_sha: str
    expected_branch: str
    strategy_execution_id: uuid.UUID
    worktree_id: uuid.UUID | None = None
    # Supervisor PER-GOAL worktree binding (mutually exclusive with worktree_id).
    # All three must be set together; Operator verifies them against supervisor_goal_leases
    # + canonical goal_worktree_path/branch — never a self-granted boolean.
    supervisor_goal_id: uuid.UUID | None = None
    supervisor_lease_id: uuid.UUID | None = None
    supervisor_lease_generation: int | None = None
    # Exact ExecutionAuthorizationEnvelope row this context was bound under. When set, every
    # Operator effect re-reads the CURRENT active envelope and refuses if it is absent or a
    # different row (founder revoke/supersede after planning / before effect).
    execution_envelope_id: uuid.UUID | None = None
    allowed_paths: tuple[str, ...] = ()
    # Empty = legacy unrestricted ONLY when this context is not envelope-governed.
    # Governed contexts (execution_envelope_id set and/or Supervisor goal binding) MUST carry
    # a non-empty capability ceiling — empty never falls back to legacy unrestricted.
    allowed_capabilities: tuple[str, ...] = ()
    remote_write_authorized: bool = False
    expected_remote_sha: str | None = None
    disposable_database_authorized: bool = False


def _supervisor_goal_binding_claimed(context: OperatorContext) -> bool:
    return (
        context.supervisor_goal_id is not None
        or context.supervisor_lease_id is not None
        or context.supervisor_lease_generation is not None
    )


def _is_governed_operator_context(context: OperatorContext) -> bool:
    """Production Supervisor / envelope-bound execution — never inherits legacy empty ceiling."""
    return (
        context.execution_envelope_id is not None
        or _supervisor_goal_binding_claimed(context)
    )


def _write_identity_present(context: OperatorContext) -> bool:
    return context.worktree_id is not None or _supervisor_goal_binding_claimed(context)


def _path_within_authorized(path: str, authorized_paths: tuple[str, ...] | list[str]) -> bool:
    pure = PurePosixPath(path)
    return any(
        pure == PurePosixPath(scope) or PurePosixPath(scope) in pure.parents
        for scope in authorized_paths
    )


def _require_live_execution_authority(db, context: OperatorContext, *, task: MainAITask) -> None:
    """Effect-time authority: planning-time authorization is not sufficient.

    If this context was bound to a specific envelope row, the CURRENT active envelope for the
    task's goal must still be that exact row. A founder revoke/supersede between Safe Planner
    ACCEPT and Operator filesystem effect must produce ZERO effect.
    """
    if context.execution_envelope_id is None:
        return
    from app.execution_envelopes import get_current_execution_envelope

    current = get_current_execution_envelope(
        db, owner_id=context.owner_id, goal_id=task.goal_id
    )
    if current is None or current.id != context.execution_envelope_id:
        raise OperatorAuthorizationError(
            "execution authorization changed since OperatorContext was bound; "
            "refusing to execute under stale authority"
        )
    authorized_paths = tuple(current.authorized_paths or ())
    if not authorized_paths:
        raise OperatorAuthorizationError(
            "current execution envelope has no authorized paths; refusing effect"
        )
    for path in context.allowed_paths:
        if not _path_within_authorized(path, authorized_paths):
            raise OperatorAuthorizationError(
                "OperatorContext path ceiling is outside the CURRENT execution envelope"
            )
    authorized_caps = tuple(current.authorized_capabilities or ())
    if not authorized_caps:
        raise OperatorAuthorizationError(
            "current execution envelope has no authorized capabilities; refusing effect"
        )
    for capability in context.allowed_capabilities:
        if capability not in authorized_caps:
            raise OperatorAuthorizationError(
                "OperatorContext capability ceiling is outside the CURRENT execution envelope"
            )


def _verify_supervisor_goal_worktree(db, context: OperatorContext, *, task: MainAITask) -> None:
    """Structurally verify the PER-GOAL Supervisor production worktree.

    Authority comes only from: active supervisor_goal_leases claim matching this worker +
    generation, task belonging to that goal, and repository_root/branch matching the
    canonical production_worktree formulas. No MainAITaskWorktree marker is consulted or
    required — that model remains PER-JOB recovery only.
    """
    from app.development_supervisor.production_worktree import (
        WORKTREE_ROOT,
        goal_branch_name,
        goal_worktree_path,
    )

    if context.worktree_id is not None:
        raise OperatorAuthorizationError(
            "ambiguous worktree class: per-job recovery and supervisor goal both claimed"
        )
    if (
        context.supervisor_goal_id is None
        or context.supervisor_lease_id is None
        or context.supervisor_lease_generation is None
    ):
        raise OperatorAuthorizationError(
            "incomplete supervisor goal worktree binding"
        )
    if task.goal_id != context.supervisor_goal_id:
        raise OperatorAuthorizationError(
            "task is not under the claimed supervisor goal"
        )

    lease = db.execute(
        select(SupervisorGoalLease).where(
            SupervisorGoalLease.id == context.supervisor_lease_id,
            SupervisorGoalLease.owner_id == context.owner_id,
            SupervisorGoalLease.goal_id == context.supervisor_goal_id,
            SupervisorGoalLease.worker_id == context.worker_id,
            SupervisorGoalLease.lease_generation == context.supervisor_lease_generation,
            SupervisorGoalLease.status == "active",
        )
    ).scalar_one_or_none()
    if lease is None or lease.expires_at <= datetime.utcnow():
        raise OperatorAuthorizationError(
            "supervisor goal lease is absent, stale, or not held by this worker"
        )

    root = context.repository_root.resolve()
    expected_path = goal_worktree_path(context.supervisor_goal_id).resolve()
    if root != expected_path:
        raise OperatorAuthorizationError(
            "repository root is not the canonical supervisor goal worktree path"
        )
    try:
        root.relative_to(WORKTREE_ROOT.resolve())
    except ValueError as exc:
        raise OperatorAuthorizationError(
            "repository root is outside the supervisor goal worktree root"
        ) from exc
    if context.expected_branch != goal_branch_name(context.supervisor_goal_id):
        raise OperatorAuthorizationError(
            "expected branch is not the canonical supervisor goal branch"
        )


@dataclass(frozen=True)
class ActionResult:
    capability: str
    result: str
    detail: dict
    trace_event_id: uuid.UUID


def redact(value: str) -> str:
    value = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    return STANDALONE_SECRET_PATTERN.sub("[REDACTED]", value)


def _redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _git(
    root: Path, args: list[str], *, timeout: int = 60
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "TZ": os.environ.get("TZ", "UTC")},
        )
    except subprocess.TimeoutExpired as exc:
        raise OperatorCommandError(f"git operation timed out after {timeout}s") from exc
    if result.returncode:
        raise OperatorCommandError(redact(result.stderr[-2000:]))
    return result


def _require_governed_capability_ceiling(context: OperatorContext) -> None:
    """EVER_GOVERNED / envelope-bound contexts never inherit legacy empty=unrestricted."""
    if _is_governed_operator_context(context) and not tuple(
        context.allowed_capabilities or ()
    ):
        raise OperatorAuthorizationError(
            "governed OperatorContext cannot inherit legacy unrestricted empty "
            "capability ceiling; refusing effect"
        )


def _require_context(db, context: OperatorContext, *, write: bool = False):
    _require_governed_capability_ceiling(context)
    task = db.execute(
        select(MainAITask).where(
            MainAITask.id == context.task_id, MainAITask.owner_id == context.owner_id
        )
    ).scalar_one_or_none()
    job = db.execute(
        select(MainAIJob).where(
            MainAIJob.id == context.job_id, MainAIJob.owner_id == context.owner_id
        )
    ).scalar_one_or_none()
    binding = db.execute(
        select(WorkStrategyExecution).where(
            WorkStrategyExecution.id == context.strategy_execution_id,
            WorkStrategyExecution.owner_id == context.owner_id,
        )
    ).scalar_one_or_none()
    if not task or not job or not binding:
        raise OperatorAuthorizationError(
            "task, job, or strategy execution is missing or cross-owner"
        )
    # Effect-time authority: planning-time envelope binding is not enough.
    _require_live_execution_authority(db, context, task=task)
    execution = None
    if binding and binding.execution_id:
        execution = db.execute(
            select(IntelligenceExecution).where(
                IntelligenceExecution.id == binding.execution_id,
                IntelligenceExecution.owner_id == context.owner_id,
                IntelligenceExecution.task_id == context.task_id,
            )
        ).scalar_one_or_none()
    if (
        task.mainai_job_id != job.id
        or execution is None
        or (execution.job_id is not None and execution.job_id != context.job_id)
    ):
        raise OperatorAuthorizationError(
            "operator context is not linked to the task's current job/execution"
        )
    if (
        job.status != MainAIJobStatus.running
        or job.locked_by != context.worker_id
        or job.lease_generation != context.lease_generation
    ):
        raise OperatorAuthorizationError("stale or absent MainAI job lease")
    # Effect-time expiry: claim-time ownership is not enough if the lease clock elapsed
    # before takeover rebinds locked_by/generation.
    if job.lease_expires_at is not None and job.lease_expires_at <= datetime.utcnow():
        raise OperatorAuthorizationError("stale or absent MainAI job lease")
    root = context.repository_root.resolve()
    if not root.is_dir() or _git(
        root, ["rev-parse", "--show-toplevel"]
    ).stdout.strip() != str(root):
        raise OperatorAuthorizationError("repository root is missing or not canonical")
    if (
        _git(root, ["branch", "--show-current"]).stdout.strip()
        != context.expected_branch
    ):
        raise OperatorAuthorizationError(
            "repository branch does not match operator context"
        )

    supervisor_claimed = _supervisor_goal_binding_claimed(context)
    if context.worktree_id is not None and supervisor_claimed:
        raise OperatorAuthorizationError(
            "ambiguous worktree class: per-job recovery and supervisor goal both claimed"
        )
    if write and context.worktree_id is None and not supervisor_claimed:
        raise OperatorAuthorizationError(
            "write capability requires a verified worktree identity "
            "(per-job recovery worktree or supervisor goal worktree)"
        )

    if context.worktree_id is not None:
        worktree = db.execute(
            select(MainAITaskWorktree).where(
                MainAITaskWorktree.id == context.worktree_id,
                MainAITaskWorktree.owner_id == context.owner_id,
                MainAITaskWorktree.task_id == context.task_id,
                MainAITaskWorktree.job_id == context.job_id,
                MainAITaskWorktree.lease_generation == context.lease_generation,
                MainAITaskWorktree.status == MainAITaskWorktreeStatus.active,
            )
        ).scalar_one_or_none()
        if (
            not worktree
            or Path(worktree.path).resolve() != root
            or not verify_worktree_ownership(worktree)
        ):
            raise WorktreeOwnershipError(
                "isolated worktree identity/marker does not match operator context"
            )
        if (
            worktree.base_sha != context.expected_base_sha
            or worktree.branch != context.expected_branch
        ):
            raise OperatorAuthorizationError(
                "worktree base or branch identity changed unexpectedly"
            )
        return task, job, binding, worktree

    if supervisor_claimed:
        _verify_supervisor_goal_worktree(db, context, task=task)
        # Shared goal worktrees advance HEAD via commit within an attempt; do not require
        # live HEAD == expected_base_sha the way read-only non-worktree contexts do.
        return task, job, binding, None

    if _git(root, ["rev-parse", "HEAD"]).stdout.strip() != context.expected_base_sha:
        raise OperatorAuthorizationError(
            "repository HEAD does not match expected base SHA"
        )
    return task, job, binding, None


def _require_capability(context: OperatorContext, capability: str) -> None:
    """Enforce OperatorContext.allowed_capabilities.

    Empty tuple preserves legacy unrestricted behavior ONLY for never-governed contexts
    (no execution_envelope_id, no Supervisor goal binding). Governed contexts with an empty
    ceiling fail closed — never fall back to unrestricted. Once a non-empty ceiling is set,
    any capability outside it fails closed here — not after a write already happened.
    """
    _require_governed_capability_ceiling(context)
    ceiling = tuple(context.allowed_capabilities or ())
    if not ceiling:
        return
    if capability not in ceiling:
        raise OperatorCapabilityMissing(
            capability,
            why_needed="capability is outside the authorized OperatorContext ceiling",
            unrelated_work_can_continue=True,
        )


def _path(context: OperatorContext, value: str, *, write: bool = False) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise OperatorPathError("path must be repository-relative and may not traverse")
    lowered = {part.lower() for part in pure.parts}
    if lowered & SENSITIVE_PARTS or any(part.startswith(".env") for part in lowered):
        raise OperatorPathError("sensitive path access denied")
    if ".git" in lowered:
        raise OperatorPathError("direct .git access denied")
    if context.allowed_paths and not any(
        pure == PurePosixPath(scope) or PurePosixPath(scope) in pure.parents
        for scope in context.allowed_paths
    ):
        raise OperatorPathError("path is outside the authorized path scope")
    result = (context.repository_root / pure).resolve()
    if (
        result != context.repository_root
        and context.repository_root not in result.parents
    ):
        raise OperatorPathError("resolved path escapes repository")
    if write and result == context.repository_root:
        raise OperatorPathError("repository root cannot be edited")
    return result


def _audit(
    db,
    context: OperatorContext,
    capability: str,
    idem: str,
    detail: dict,
    result: str,
    started: float,
):
    action_types = {
        "repo_status": "repository_map_inspected",
        "repo_branch": "repository_map_inspected",
        "repo_head": "git_history_inspected",
        "repo_merge_base": "git_history_inspected",
        "repo_diff": "repository_map_inspected",
        "repo_log": "git_history_inspected",
        "list_files": "repository_map_inspected",
        "search_text": "symbol_searched",
        "read_file": "file_read",
        "inspect_git_history": "git_history_inspected",
        "run_focused_test": "focused_test_run",
        "run_full_suite": "full_suite_run",
        "run_static_check": "static_analysis",
        "run_migration_check": "migration_test",
        "capability_missing": "work_escalated",
    }
    trace_action = action_types.get(capability, "change_attempted")
    safe_detail = _redact_value(
        {"operator_capability": capability, "operator_result": result, **detail}
    )
    trace_result = {"timeout": "failed", "capability_missing": "deferred"}.get(
        result, result
    )
    return record_trace_event(
        db,
        owner_id=context.owner_id,
        strategy_execution_id=context.strategy_execution_id,
        action_type=trace_action,
        tool_identity="life_development_operator",
        target_type="repository",
        target_ref=str(context.repository_root),
        action_detail=safe_detail,
        result=trace_result,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        basis="deterministic",
        provenance={"task_id": str(context.task_id), "job_id": str(context.job_id)},
        idempotency_key=idem,
    )


def _replay(
    db,
    context: OperatorContext,
    capability: str,
    idempotency_key: str,
    *,
    expected_detail: dict | None = None,
):
    event = db.execute(
        select(WorkTraceEvent).where(
            WorkTraceEvent.owner_id == context.owner_id,
            WorkTraceEvent.strategy_execution_id == context.strategy_execution_id,
            WorkTraceEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if event is None:
        return None
    if event.action_detail.get("operator_capability") != capability:
        raise OperatorAuthorizationError(
            "idempotency key reused for another capability"
        )
    if expected_detail and any(
        event.action_detail.get(key) != _redact_value(value)
        for key, value in expected_detail.items()
    ):
        raise OperatorAuthorizationError(
            "idempotency key reused with conflicting action semantics"
        )
    return ActionResult(
        capability,
        event.action_detail.get("operator_result", event.result),
        event.action_detail,
        event.id,
    )


def inspect_repository(
    db,
    context: OperatorContext,
    capability: str,
    *,
    idempotency_key: str,
    other_ref: str | None = None,
):
    started = time.monotonic()
    _require_context(db, context)
    commands = {
        "repo_status": ["status", "--porcelain"],
        "repo_branch": ["branch", "--show-current"],
        "repo_head": ["rev-parse", "HEAD"],
        "repo_diff": ["diff", "--stat"],
        "repo_log": ["log", "-n", "20", "--oneline", "--decorate=no"],
    }
    if capability == "repo_merge_base":
        if not other_ref or not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", other_ref):
            raise OperatorCommandError("merge-base requires a bounded git ref")
        args = ["merge-base", "HEAD", other_ref]
    elif capability in commands:
        args = commands[capability]
    else:
        raise OperatorCapabilityMissing(
            capability,
            why_needed="no structured repository implementation",
            unrelated_work_can_continue=True,
        )
    output = redact(_git(context.repository_root, args).stdout[:64_000])
    if capability in {"repo_branch", "repo_head", "repo_merge_base"}:
        output = output.strip()
    event = _audit(
        db,
        context,
        capability,
        idempotency_key,
        {"output": output},
        "succeeded",
        started,
    )
    return ActionResult(capability, "succeeded", {"output": output}, event.id)


def list_files(
    db,
    context: OperatorContext,
    *,
    scope: str = ".",
    limit: int = 1000,
    idempotency_key: str,
):
    started = time.monotonic()
    _require_context(db, context)
    root = _path(context, scope)
    files = []
    for item in sorted(root.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            relative = item.relative_to(context.repository_root).as_posix()
            try:
                _path(context, relative)
            except OperatorPathError:
                continue
            files.append(relative)
            if len(files) >= min(max(limit, 1), 5000):
                break
    event = _audit(
        db,
        context,
        "list_files",
        idempotency_key,
        {"scope": scope, "count": len(files)},
        "succeeded",
        started,
    )
    return ActionResult("list_files", "succeeded", {"files": files}, event.id)


def search_text(
    db,
    context: OperatorContext,
    *,
    query: str,
    scope: str = ".",
    limit: int = 200,
    idempotency_key: str,
):
    started = time.monotonic()
    _require_context(db, context)
    target = _path(context, scope)
    if not query or len(query) > 512 or "\x00" in query:
        raise OperatorCommandError("search query is empty or exceeds bounds")
    try:
        result = subprocess.run(
            [
                "rg",
                "--line-number",
                "--no-heading",
                "--color",
                "never",
                "--fixed-strings",
                "--",
                query,
                str(target),
            ],
            cwd=context.repository_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "TZ": os.environ.get("TZ", "UTC"),
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise OperatorCommandError("bounded text search timed out") from exc
    except FileNotFoundError as exc:
        raise OperatorCapabilityMissing(
            "search_text",
            why_needed="the deterministic rg search tool is unavailable",
            unrelated_work_can_continue=True,
        ) from exc
    if result.returncode not in {0, 1}:
        raise OperatorCommandError(redact(result.stderr[-2000:]))
    matches = [
        redact(line) for line in result.stdout.splitlines()[: min(max(limit, 1), 1000)]
    ]
    event = _audit(
        db,
        context,
        "search_text",
        idempotency_key,
        {"query": query, "scope": scope, "matches": len(matches)},
        "succeeded",
        started,
    )
    return ActionResult("search_text", "succeeded", {"matches": matches}, event.id)


def read_file(
    db,
    context: OperatorContext,
    *,
    path: str,
    max_bytes: int = 128_000,
    idempotency_key: str,
):
    started = time.monotonic()
    _require_context(db, context)
    target = _path(context, path)
    if not target.is_file() or target.is_symlink():
        raise OperatorPathError("file is missing, not regular, or symlinked")
    data = target.read_bytes()
    if len(data) > min(max(max_bytes, 1), 1_000_000) or b"\x00" in data:
        raise OperatorPathError("file exceeds read bound or is binary")
    try:
        content = redact(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise OperatorPathError("file is not bounded UTF-8 text") from exc
    event = _audit(
        db,
        context,
        "read_file",
        idempotency_key,
        {"path": path, "bytes": len(data)},
        "succeeded",
        started,
    )
    return ActionResult(
        "read_file", "succeeded", {"path": path, "content": content}, event.id
    )


def write_file(
    db,
    context: OperatorContext,
    *,
    path: str,
    content: str | None,
    expected_sha256: str | None,
    idempotency_key: str,
    delete: bool = False,
):
    started = time.monotonic()
    _require_context(db, context, write=True)
    target = _path(context, path, write=True)
    existing = (
        target.read_bytes()
        if target.exists() and target.is_file() and not target.is_symlink()
        else None
    )
    capability = (
        "delete_file"
        if delete
        else ("create_file" if expected_sha256 is None else "patch_file")
    )
    requested_after = (
        None if delete else hashlib.sha256((content or "").encode()).hexdigest()
    )
    replay = _replay(
        db,
        context,
        capability,
        idempotency_key,
        expected_detail={
            "path": path,
            "before_sha256": expected_sha256,
            "after_sha256": requested_after,
        },
    )
    if replay:
        return replay
    before_hash = hashlib.sha256(existing).hexdigest() if existing is not None else None
    if before_hash != expected_sha256:
        raise OperatorAuthorizationError("expected-before hash mismatch")
    if delete:
        if existing is None:
            raise OperatorPathError("delete target does not exist")
        target.unlink()
        after_hash = None
        capability = "delete_file"
    else:
        if content is None or len(content.encode()) > 1_000_000:
            raise OperatorPathError("write content is missing or exceeds bound")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        after_hash = hashlib.sha256(content.encode()).hexdigest()
        capability = "patch_file" if existing is not None else "create_file"
    event = _audit(
        db,
        context,
        capability,
        idempotency_key,
        {"path": path, "before_sha256": before_hash, "after_sha256": after_hash},
        "succeeded",
        started,
    )
    return ActionResult(
        capability,
        "succeeded",
        {"path": path, "before_sha256": before_hash, "after_sha256": after_hash},
        event.id,
    )


def run_profile(
    db,
    context: OperatorContext,
    *,
    profile_name: str,
    arguments: list[str],
    idempotency_key: str,
    environment: dict[str, str] | None = None,
):
    started = time.monotonic()
    _require_context(db, context, write=True)
    profile = COMMAND_PROFILES.get(profile_name)
    if not profile:
        raise OperatorCommandError("command profile is not allowlisted")
    environment = environment or {}
    forbidden_env = set(environment) - SAFE_ENV_NAMES
    if forbidden_env:
        raise OperatorCommandError("environment variable is not allowlisted")
    if any(
        not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 512
        for arg in arguments
    ):
        raise OperatorCommandError("command argument is malformed")
    replay = _replay(
        db,
        context,
        profile.capability,
        idempotency_key,
        expected_detail={"profile": profile_name, "arguments": arguments},
    )
    if replay:
        return replay
    if profile_name == "focused_pytest":
        if len(arguments) != 1:
            raise OperatorCommandError("focused pytest accepts exactly one target")
        _path(context, arguments[0].split("::", 1)[0])
    elif profile_name == "full_backend_pytest" and arguments:
        raise OperatorCommandError("full suite profile accepts no arguments")
    elif profile_name == "ruff_check":
        for arg in arguments:
            if arg.startswith("-"):
                raise OperatorCommandError("ruff options are fixed by profile")
            _path(context, arg)
    elif profile_name == "git_diff_check" and arguments:
        raise OperatorCommandError("git diff check profile accepts no arguments")
    elif profile_name == "alembic":
        if not context.disposable_database_authorized:
            raise OperatorAuthorizationError(
                "migration commands require an explicitly authorized disposable database"
            )
        if arguments not in (["upgrade", "head"], ["downgrade", "-1"]):
            raise OperatorCommandError("alembic arguments are not allowlisted")
    env = {name: os.environ.get(name, "") for name in SAFE_ENV_NAMES}
    env.update(environment)
    try:
        process = subprocess.run(
            [*profile.argv_prefix, *arguments],
            cwd=context.repository_root,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            check=False,
            env=env,
        )
        result_class = "succeeded" if process.returncode == 0 else "failed"
        output = redact((process.stdout + process.stderr)[-profile.max_output_bytes :])
        detail = {
            "profile": profile_name,
            "arguments": arguments,
            "returncode": process.returncode,
            "output": output,
        }
    except subprocess.TimeoutExpired:
        result_class, detail = (
            "timeout",
            {"profile": profile_name, "arguments": arguments, "error": "timeout"},
        )
    event = _audit(
        db, context, profile.capability, idempotency_key, detail, result_class, started
    )
    return ActionResult(profile.capability, result_class, detail, event.id)


def checkpoint_operator_progress(
    db,
    context: OperatorContext,
    *,
    completed_action_ids: list[uuid.UUID],
    next_phase: str,
    unresolved_failures: list[str],
):
    task, _, _, worktree = _require_context(
        db, context, write=_write_identity_present(context)
    )
    goal = db.execute(
        select(MainAIGoal).where(
            MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == context.owner_id
        )
    ).scalar_one()
    status = _git(context.repository_root, ["status", "--porcelain"]).stdout
    head = _git(context.repository_root, ["rev-parse", "HEAD"]).stdout.strip()
    return record_checkpoint(
        db,
        task=task,
        goal=goal,
        job_id=context.job_id,
        step="development_operator",
        data={
            "repository": str(context.repository_root),
            "base_sha": context.expected_base_sha,
            "branch": context.expected_branch,
            "worktree_id": str(worktree.id) if worktree else None,
            "supervisor_goal_id": (
                str(context.supervisor_goal_id) if context.supervisor_goal_id else None
            ),
            "completed_action_ids": [str(value) for value in completed_action_ids],
            "next_phase": next_phase,
            "unresolved_failures": unresolved_failures,
            "dirty": bool(status.strip()),
            "head_sha": head,
        },
    )


def stage_scoped_changes(
    db,
    context: OperatorContext,
    *,
    paths: list[str],
    idempotency_key: str,
):
    started = time.monotonic()
    _require_context(db, context, write=True)
    if not paths or len(paths) > 100:
        raise OperatorPathError("one to 100 scoped paths are required")
    normalized = []
    for path in paths:
        _path(context, path, write=True)
        normalized.append(PurePosixPath(path).as_posix())
    normalized = sorted(set(normalized))
    replay = _replay(
        db,
        context,
        "stage_scoped_changes",
        idempotency_key,
        expected_detail={"paths": normalized},
    )
    if replay:
        return replay
    _git(context.repository_root, ["add", "--", *normalized])
    event = _audit(
        db,
        context,
        "stage_scoped_changes",
        idempotency_key,
        {"paths": normalized},
        "succeeded",
        started,
    )
    return ActionResult(
        "stage_scoped_changes", "succeeded", {"paths": normalized}, event.id
    )


def commit_changes(db, context: OperatorContext, *, message: str, idempotency_key: str):
    started = time.monotonic()
    _, _, _, worktree = _require_context(db, context, write=True)
    message_sha256 = hashlib.sha256(message.encode()).hexdigest()
    replay = _replay(
        db,
        context,
        "commit_scoped_changes",
        idempotency_key,
        expected_detail={"message_sha256": message_sha256},
    )
    if replay:
        return replay
    if not message.strip() or len(message) > 200 or "\n" in message:
        raise OperatorCommandError("commit message is invalid")
    verification = latest_checkpoint_for_step(
        db,
        task_id=context.task_id,
        job_id=context.job_id,
        step="verification",
    )
    if not verification or not verification.executor_state.get("verification", {}).get(
        "passed"
    ):
        raise OperatorVerificationRequired(
            "required verification checkpoint is absent or failed"
        )
    _git(context.repository_root, ["diff", "--check", "--cached"])
    staged_paths = _git(
        context.repository_root,
        ["diff", "--cached", "--name-only", "--diff-filter=ACMRD"],
    ).stdout.splitlines()
    if not staged_paths:
        raise OperatorAuthorizationError("no explicitly staged changes exist")
    for path in staged_paths:
        _path(context, path, write=True)
    diff = _git(context.repository_root, ["diff", "--cached", "--no-ext-diff"])
    if SECRET_PATTERN.search(diff.stdout) or STANDALONE_SECRET_PATTERN.search(
        diff.stdout
    ):
        raise OperatorAuthorizationError("secret-like content detected in diff")
    _git(context.repository_root, ["commit", "-q", "-m", message])
    sha = _git(context.repository_root, ["rev-parse", "HEAD"]).stdout.strip()
    if worktree is not None:
        worktree.current_commit = sha
        db.flush()
    event = _audit(
        db,
        context,
        "commit_scoped_changes",
        idempotency_key,
        {"commit_sha": sha, "message_sha256": message_sha256},
        "succeeded",
        started,
    )
    return ActionResult(
        "commit_scoped_changes", "succeeded", {"commit_sha": sha}, event.id
    )


async def push_branch(db, context: OperatorContext, *, idempotency_key: str):
    started = time.monotonic()
    _, _, _, worktree = _require_context(db, context, write=True)
    if worktree is None:
        raise OperatorAuthorizationError(
            "push_branch requires a per-job recovery worktree; "
            "supervisor goal worktrees are local-only"
        )
    if not context.remote_write_authorized:
        raise OperatorAuthorizationError("REMOTE_WRITE authorization is required")
    replay = _replay(
        db,
        context,
        "push_branch",
        idempotency_key,
        expected_detail={"branch": context.expected_branch, "force": False},
    )
    if replay:
        return replay
    if worktree.branch != context.expected_branch or not worktree.current_commit:
        raise OperatorAuthorizationError("branch/commit identity is not ready for push")
    remote_sha = fetch_remote_branch(worktree)
    if remote_sha != context.expected_remote_sha:
        raise OperatorAuthorizationError("remote branch state changed unexpectedly")
    sha = await push_worktree_branch(worktree)
    event = _audit(
        db,
        context,
        "push_branch",
        idempotency_key,
        {"branch": worktree.branch, "commit_sha": sha, "force": False},
        "succeeded",
        started,
    )
    return ActionResult(
        "push_branch", "succeeded", {"commit_sha": sha, "force": False}, event.id
    )


def capability_missing(
    db,
    context: OperatorContext,
    *,
    capability: str,
    why_needed: str,
    unrelated_work_can_continue: bool,
    idempotency_key: str,
):
    started = time.monotonic()
    _require_context(db, context)
    event = _audit(
        db,
        context,
        "capability_missing",
        idempotency_key,
        {
            "requested_capability": capability,
            "why_needed": why_needed,
            "unrelated_work_can_continue": unrelated_work_can_continue,
        },
        "deferred",
        started,
    )
    return ActionResult(
        "capability_missing",
        "capability_missing",
        {
            "requested_capability": capability,
            "why_needed": why_needed,
            "unrelated_work_can_continue": unrelated_work_can_continue,
        },
        event.id,
    )
