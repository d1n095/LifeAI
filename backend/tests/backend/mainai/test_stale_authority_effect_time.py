"""Effect-time authority: planning-time envelope binding is not enough.

Adversarial chain:
  envelope A active
  → OperatorContext bound under A (as after Safe Planner ACCEPT)
  → BEFORE filesystem effect, founder supersedes/revokes A
  → Operator MUST refuse under stale A
  → ZERO filesystem effect
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.development_operator.service import (
    OperatorAuthorizationError,
    OperatorContext,
    _require_capability,
    write_file,
)
from app.execution_envelopes import (
    authorize_execution_scope,
    get_current_execution_envelope,
    propose_execution_scope,
)
from app.intelligence_governance import record_execution
from app.mainai_execution.approval import grant_task_approval
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAIPlan, MainAITask
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree
from app.models.user import User
from app.work_intelligence import bind_strategy_execution, create_strategy


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _foundation_with_envelope(db, tmp_path):
    owner = User(
        email=f"stale-auth-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    goal = MainAIGoal(
        owner_id=owner.id,
        title="stale authority",
        original_instruction="safe repository work under envelope A",
        created_by="test",
        approval_policy="autonomous_development_work",
        status=MainAIGoalStatus.running,
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        rationale="test",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="edit repo",
        task_type="repo_edit",
        verification_plan=[{"kind": "targeted_tests"}],
    )
    db.add(task)
    db.flush()
    grant_task_approval(db, task=task, approved_by="test")
    job = MainAIJob(
        owner_id=owner.id,
        job_type="task_execution",
        status=MainAIJobStatus.running,
        locked_by="worker-1",
        lease_generation=7,
        started_at=datetime.utcnow(),
        last_heartbeat_at=datetime.utcnow(),
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        created_by="test",
    )
    db.add(job)
    db.flush()
    task.mainai_job_id = job.id

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "operator-work")
    _git(repo, "config", "user.email", "operator@example.com")
    _git(repo, "config", "user.name", "Operator Test")
    (repo / "safe.txt").write_text("canonical\n", encoding="utf-8")
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    execution = record_execution(
        db,
        owner_id=owner.id,
        task_id=task.id,
        job_id=job.id,
        provider=None,
        model=None,
        idempotency_key=f"stale-auth-execution-{uuid.uuid4()}",
    )
    strategy = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key="stale-auth-operator",
        version=1,
        work_category="repository_work",
        idempotency_key=f"stale-auth-strategy-{uuid.uuid4()}",
    )
    binding = bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        execution_id=execution.id,
        idempotency_key=f"stale-auth-binding-{uuid.uuid4()}",
    )
    marker_token = "b" * 32
    (repo / ".mainai_worktree_owner.json").write_text(
        json.dumps(
            {
                "task_id": str(task.id),
                "job_id": str(job.id),
                "marker_token": marker_token,
            }
        )
    )
    (repo / ".git" / "info" / "exclude").write_text(".mainai_worktree_owner.json\n")
    worktree = MainAITaskWorktree(
        task_id=task.id,
        owner_id=owner.id,
        job_id=job.id,
        lease_generation=7,
        executor_id="worker-1",
        repo="local/test",
        base_sha=base_sha,
        branch="operator-work",
        path=str(repo),
        marker_token=marker_token,
    )
    db.add(worktree)
    db.flush()

    proposal = propose_execution_scope(
        db,
        owner_id=owner.id,
        goal_id=goal.id,
        idempotency_key=f"stale-auth-prop-a-{uuid.uuid4()}",
        proposed_paths=["safe.txt"],
        proposed_capabilities=["read_file", "patch_file"],
        proposed_risk="low",
        repository_identity=str(repo.resolve()),
    )
    _, envelope_a = authorize_execution_scope(
        db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["safe.txt"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"stale-auth-env-a-{uuid.uuid4()}",
    )
    db.flush()

    context = OperatorContext(
        owner_id=owner.id,
        task_id=task.id,
        job_id=job.id,
        worker_id="worker-1",
        lease_generation=7,
        repository_root=repo,
        expected_base_sha=base_sha,
        expected_branch="operator-work",
        strategy_execution_id=binding.id,
        worktree_id=worktree.id,
        execution_envelope_id=envelope_a.id,
        allowed_paths=("safe.txt",),
        allowed_capabilities=("read_file", "patch_file"),
    )
    return owner, goal, task, job, worktree, context, envelope_a, repo


def test_stale_envelope_a_after_supersede_produces_zero_filesystem_effect(
    superuser_db, tmp_path
):
    """Plan accepted under A; founder supersedes A before write → refuse + unchanged file."""
    owner, goal, _, _, _, context, envelope_a, repo = _foundation_with_envelope(
        superuser_db, tmp_path
    )
    original = (repo / "safe.txt").read_text(encoding="utf-8")
    content_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # Prove write under live A succeeds (authority still current).
    ok = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="under-A\n",
        expected_sha256=content_hash,
        idempotency_key="stale-auth-write-under-a",
    )
    assert ok.result == "succeeded"
    assert (repo / "safe.txt").read_text(encoding="utf-8") == "under-A\n"

    # Founder supersedes A with B (same paths — identity change alone must invalidate A).
    proposal_b = propose_execution_scope(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        idempotency_key=f"stale-auth-prop-b-{uuid.uuid4()}",
        proposed_paths=["safe.txt"],
        proposed_capabilities=["read_file", "patch_file"],
        proposed_risk="low",
        repository_identity=str(repo.resolve()),
    )
    _, envelope_b = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_b.id,
        authorized_by="founder",
        authorized_paths=["safe.txt"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"stale-auth-env-b-{uuid.uuid4()}",
    )
    superuser_db.flush()
    assert envelope_b.id != envelope_a.id
    current = get_current_execution_envelope(
        superuser_db, owner_id=owner.id, goal_id=goal.id
    )
    assert current is not None and current.id == envelope_b.id

    before_stale = (repo / "safe.txt").read_text(encoding="utf-8")
    stale_hash = hashlib.sha256(before_stale.encode("utf-8")).hexdigest()
    with pytest.raises(OperatorAuthorizationError, match="stale authority"):
        write_file(
            superuser_db,
            context,  # still bound to envelope A
            path="safe.txt",
            content="under-stale-A\n",
            expected_sha256=stale_hash,
            idempotency_key="stale-auth-write-after-supersede",
        )
    assert (repo / "safe.txt").read_text(encoding="utf-8") == before_stale


def test_revoked_envelope_with_no_replacement_produces_zero_filesystem_effect(
    superuser_db, tmp_path
):
    """Founder clears current authority (no active envelope) → refuse + unchanged file."""
    _, goal, _, _, _, context, envelope_a, repo = _foundation_with_envelope(
        superuser_db, tmp_path
    )
    envelope_a.status = "superseded"
    superuser_db.flush()
    assert (
        get_current_execution_envelope(
            superuser_db, owner_id=context.owner_id, goal_id=goal.id
        )
        is None
    )

    original = (repo / "safe.txt").read_text(encoding="utf-8")
    with pytest.raises(OperatorAuthorizationError, match="stale authority"):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content="after-revoke\n",
            expected_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            idempotency_key="stale-auth-write-after-revoke",
        )
    assert (repo / "safe.txt").read_text(encoding="utf-8") == original


def test_governed_empty_capability_ceiling_fails_closed(tmp_path):
    """execution_envelope_id set + empty allowed_capabilities → never legacy unrestricted."""
    governed_empty = OperatorContext(
        owner_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        worker_id="cap-test",
        lease_generation=1,
        repository_root=tmp_path,
        expected_base_sha="0" * 40,
        expected_branch="main",
        strategy_execution_id=uuid.uuid4(),
        execution_envelope_id=uuid.uuid4(),
        allowed_paths=("calculator.py",),
        allowed_capabilities=(),
    )
    with pytest.raises(OperatorAuthorizationError, match="empty capability ceiling"):
        _require_capability(governed_empty, "patch_file")

    # Never-governed legacy callers with empty ceiling remain unrestricted.
    legacy = replace(governed_empty, execution_envelope_id=None)
    _require_capability(legacy, "patch_file")


def test_supervisor_goal_binding_empty_capability_ceiling_fails_closed(tmp_path):
    """Supervisor goal binding alone is governed — empty ceiling must not unrestricted."""
    governed = OperatorContext(
        owner_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        worker_id="cap-test",
        lease_generation=1,
        repository_root=tmp_path,
        expected_base_sha="0" * 40,
        expected_branch="main",
        strategy_execution_id=uuid.uuid4(),
        supervisor_goal_id=uuid.uuid4(),
        supervisor_lease_id=uuid.uuid4(),
        supervisor_lease_generation=1,
        allowed_paths=("calculator.py",),
        allowed_capabilities=(),
    )
    with pytest.raises(OperatorAuthorizationError, match="empty capability ceiling"):
        _require_capability(governed, "read_file")
