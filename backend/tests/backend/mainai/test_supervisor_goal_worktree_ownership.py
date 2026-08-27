"""Prove Supervisor PER-GOAL worktree write auth is NOT MainAITaskWorktree.

#167 initially stamped `.mainai_worktree_owner.json` + MainAITaskWorktree rows onto the
shared goal worktree. That mixes two intentional models:

A) production_worktree — PER-GOAL, shared across tasks/jobs/ticks, disposable
B) MainAITaskWorktree — PER-JOB recovery identity (task_id/job_id/marker_token)

Job B overwriting the shared marker makes Job A unverifiable — a structural ownership
corruption, not a style issue.

These tests prove the conflict exists for model B-on-shared-path, then prove production
Operator write auth uses supervisor lease + canonical path/branch instead.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_operator.service import (
    OperatorAuthorizationError,
    OperatorContext,
    write_file,
)
from app.development_supervisor.lease import claim_supervisor_goal_lease
from app.development_supervisor.production_worktree import (
    goal_branch_name,
    goal_worktree_path,
)
from app.intelligence_governance import record_execution
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.worktree import verify_worktree_ownership
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
from app.models.user import User
from app.work_intelligence import bind_strategy_execution, create_strategy


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_worktree_root(tmp_path, monkeypatch):
    import app.development_supervisor.production_worktree as module

    monkeypatch.setattr(module, "WORKTREE_ROOT", tmp_path / "supervisor-goal-worktrees")


def test_shared_path_per_job_markers_invalidate_prior_job_ownership(tmp_path, superuser_db):
    """Negative control: two MainAITaskWorktree rows on ONE shared directory cannot both verify.

    This is the lifecycle #167's first worktree fix accidentally created.
    """
    owner = User(
        email=f"ow-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    superuser_db.add(owner)
    superuser_db.flush()
    goal = MainAIGoal(
        owner_id=owner.id,
        title="shared wt",
        original_instruction="x",
        created_by="test",
        approval_policy="autonomous_development_work",
    )
    superuser_db.add(goal)
    superuser_db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        rationale="t",
        created_by="test",
    )
    superuser_db.add(plan)
    superuser_db.flush()

    shared = tmp_path / "shared-goal-wt"
    shared.mkdir()
    _git(shared, "init", "-q", "-b", "goal-branch")
    _git(shared, "config", "user.email", "t@t.local")
    _git(shared, "config", "user.name", "t")
    (shared / "f.txt").write_text("a\n", encoding="utf-8")
    _git(shared, "add", "f.txt")
    _git(shared, "commit", "-q", "-m", "seed")
    base = _git(shared, "rev-parse", "HEAD")

    rows = []
    for idx in (1, 2):
        task = MainAITask(
            owner_id=owner.id,
            goal_id=goal.id,
            plan_id=plan.id,
            description=f"task-{idx}",
            task_type="repo_edit",
            verification_plan=[{"kind": "targeted_tests"}],
        )
        superuser_db.add(task)
        superuser_db.flush()
        job = MainAIJob(
            owner_id=owner.id,
            job_type="task_execution",
            status=MainAIJobStatus.running,
            locked_by=f"w-{idx}",
            lease_generation=1,
            started_at=datetime.utcnow(),
            last_heartbeat_at=datetime.utcnow(),
            lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
            created_by="test",
        )
        superuser_db.add(job)
        superuser_db.flush()
        task.mainai_job_id = job.id
        token = hashlib.sha256(f"{task.id}:{job.id}".encode()).hexdigest()[:32]
        marker = {
            "task_id": str(task.id),
            "job_id": str(job.id),
            "marker_token": token,
        }
        (shared / ".mainai_worktree_owner.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        wt = MainAITaskWorktree(
            task_id=task.id,
            owner_id=owner.id,
            job_id=job.id,
            lease_generation=1,
            executor_id=f"w-{idx}",
            repo="local/shared",
            base_sha=base,
            branch="goal-branch",
            path=str(shared.resolve()),
            marker_token=token,
            status=MainAITaskWorktreeStatus.active,
        )
        superuser_db.add(wt)
        superuser_db.flush()
        rows.append(wt)

    row_a, row_b = rows
    # After B wrote its marker into the shared path, only B verifies.
    assert verify_worktree_ownership(row_b) is True
    assert verify_worktree_ownership(row_a) is False
    assert row_a.status == MainAITaskWorktreeStatus.active  # stale active row remains
    assert Path(row_a.path).resolve() == Path(row_b.path).resolve()


def _supervisor_write_foundation(db, tmp_path, *, monkeypatch):
    import app.development_supervisor.production_worktree as wt_module

    owner = User(
        email=f"sg-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    goal = MainAIGoal(
        owner_id=owner.id,
        title="supervisor write",
        original_instruction="edit",
        created_by="test",
        approval_policy="autonomous_development_work",
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        rationale="t",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="edit",
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
        locked_by="supervisor-worker",
        lease_generation=3,
        started_at=datetime.utcnow(),
        last_heartbeat_at=datetime.utcnow(),
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        created_by="test",
    )
    db.add(job)
    db.flush()
    task.mainai_job_id = job.id

    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope

    proposal = propose_execution_scope(
        db, owner_id=owner.id, goal_id=goal.id, idempotency_key=f"sg-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["safe.txt"],
        authorized_capabilities=["patch_file", "read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"sg-env-{uuid.uuid4()}",
    )
    lease_id, lease_gen = claim_supervisor_goal_lease(
        db,
        owner_id=owner.id,
        goal_id=goal.id,
        envelope_id=envelope.id,
        worker_id="supervisor-worker",
        lease_seconds=300,
    )
    assert lease_id is not None

    repo = goal_worktree_path(goal.id)
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", goal_branch_name(goal.id))
    _git(repo, "config", "user.email", "op@test.local")
    _git(repo, "config", "user.name", "Op")
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
        idempotency_key=f"sg-exec-{uuid.uuid4()}",
    )
    strategy = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key="governed-operator",
        version=1,
        work_category="repository_work",
        idempotency_key=f"sg-strategy-{uuid.uuid4()}",
    )
    binding = bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        execution_id=execution.id,
        idempotency_key=f"sg-binding-{uuid.uuid4()}",
    )
    context = OperatorContext(
        owner_id=owner.id,
        task_id=task.id,
        job_id=job.id,
        worker_id="supervisor-worker",
        lease_generation=3,
        repository_root=repo,
        expected_base_sha=base_sha,
        expected_branch=goal_branch_name(goal.id),
        strategy_execution_id=binding.id,
        supervisor_goal_id=goal.id,
        supervisor_lease_id=lease_id,
        supervisor_lease_generation=lease_gen,
        allowed_paths=("safe.txt",),
        allowed_capabilities=("patch_file", "read_file"),
    )
    return owner, goal, task, job, context, repo


def test_supervisor_goal_write_auth_via_lease_without_per_job_marker(
    superuser_db, tmp_path, monkeypatch
):
    _, goal, _, job, context, repo = _supervisor_write_foundation(
        superuser_db, tmp_path, monkeypatch=monkeypatch
    )
    assert not (repo / ".mainai_worktree_owner.json").exists()
    assert (
        superuser_db.execute(
            select(MainAITaskWorktree).where(MainAITaskWorktree.job_id == job.id)
        ).scalar_one_or_none()
        is None
    )

    result = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="updated\n",
        expected_sha256=hashlib.sha256(b"canonical\n").hexdigest(),
        idempotency_key="sg-write-1",
    )
    assert result.result == "succeeded"
    assert (repo / "safe.txt").read_text(encoding="utf-8") == "updated\n"
    assert not (repo / ".mainai_worktree_owner.json").exists()
    assert (
        superuser_db.execute(
            select(MainAITaskWorktree).where(MainAITaskWorktree.job_id == job.id)
        ).scalar_one_or_none()
        is None
    )


def test_supervisor_write_rejects_incomplete_or_self_authorize_binding(
    superuser_db, tmp_path, monkeypatch
):
    from dataclasses import replace

    _, goal, _, _, context, repo = _supervisor_write_foundation(
        superuser_db, tmp_path, monkeypatch=monkeypatch
    )
    content_hash = hashlib.sha256(b"canonical\n").hexdigest()

    # Incomplete lease binding — not a boolean self-grant.
    incomplete = replace(
        context,
        supervisor_lease_id=None,
        supervisor_lease_generation=None,
    )
    with pytest.raises(OperatorAuthorizationError, match="incomplete supervisor goal"):
        write_file(
            superuser_db,
            incomplete,
            path="safe.txt",
            content="x\n",
            expected_sha256=content_hash,
            idempotency_key="sg-incomplete",
        )

    # Wrong path (not canonical goal_worktree_path).
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", context.expected_branch)
    _git(other, "config", "user.email", "t@t.local")
    _git(other, "config", "user.name", "t")
    (other / "safe.txt").write_text("canonical\n", encoding="utf-8")
    _git(other, "add", "safe.txt")
    _git(other, "commit", "-q", "-m", "seed")
    wrong_path = replace(context, repository_root=other, expected_base_sha=_git(other, "rev-parse", "HEAD"))
    with pytest.raises(OperatorAuthorizationError, match="canonical supervisor goal"):
        write_file(
            superuser_db,
            wrong_path,
            path="safe.txt",
            content="x\n",
            expected_sha256=content_hash,
            idempotency_key="sg-wrong-path",
        )

    # Ambiguous: both worktree_id and supervisor binding.
    ambiguous = replace(context, worktree_id=uuid.uuid4())
    with pytest.raises(OperatorAuthorizationError, match="ambiguous worktree class"):
        write_file(
            superuser_db,
            ambiguous,
            path="safe.txt",
            content="x\n",
            expected_sha256=content_hash,
            idempotency_key="sg-ambiguous",
        )


def test_two_jobs_same_goal_worktree_both_write_without_ownership_collision(
    superuser_db, tmp_path, monkeypatch
):
    """Task/job A then B under the same goal: shared path reused; no marker rewrite; both write."""
    from dataclasses import replace

    owner, goal, task_a, job_a, context_a, repo = _supervisor_write_foundation(
        superuser_db, tmp_path, monkeypatch=monkeypatch
    )
    write_file(
        superuser_db,
        context_a,
        path="safe.txt",
        content="from-a\n",
        expected_sha256=hashlib.sha256(b"canonical\n").hexdigest(),
        idempotency_key="a-write",
    )
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-q", "-m", "task-a")
    head_after_a = _git(repo, "rev-parse", "HEAD")

    plan = superuser_db.execute(
        select(MainAIPlan).where(MainAIPlan.goal_id == goal.id)
    ).scalar_one()
    task_b = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="edit-b",
        task_type="repo_edit",
        verification_plan=[{"kind": "targeted_tests"}],
    )
    superuser_db.add(task_b)
    superuser_db.flush()
    grant_task_approval(superuser_db, task=task_b, approved_by="test")
    job_b = MainAIJob(
        owner_id=owner.id,
        job_type="task_execution",
        status=MainAIJobStatus.running,
        locked_by="supervisor-worker",
        lease_generation=1,
        started_at=datetime.utcnow(),
        last_heartbeat_at=datetime.utcnow(),
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        created_by="test",
    )
    superuser_db.add(job_b)
    superuser_db.flush()
    task_b.mainai_job_id = job_b.id
    execution = record_execution(
        superuser_db,
        owner_id=owner.id,
        task_id=task_b.id,
        job_id=job_b.id,
        provider=None,
        model=None,
        idempotency_key=f"sg-exec-b-{uuid.uuid4()}",
    )
    strategy = create_strategy(
        superuser_db,
        owner_id=owner.id,
        strategy_key="governed-operator-b",
        version=1,
        work_category="repository_work",
        idempotency_key=f"sg-strategy-b-{uuid.uuid4()}",
    )
    binding = bind_strategy_execution(
        superuser_db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        execution_id=execution.id,
        idempotency_key=f"sg-binding-b-{uuid.uuid4()}",
    )
    # Same active supervisor lease + same shared goal path (production reuse).
    context_b = replace(
        context_a,
        task_id=task_b.id,
        job_id=job_b.id,
        lease_generation=1,
        expected_base_sha=head_after_a,
        strategy_execution_id=binding.id,
    )
    write_file(
        superuser_db,
        context_b,
        path="safe.txt",
        content="from-b\n",
        expected_sha256=hashlib.sha256(b"from-a\n").hexdigest(),
        idempotency_key="b-write",
    )
    assert (repo / "safe.txt").read_text(encoding="utf-8") == "from-b\n"
    assert not (repo / ".mainai_worktree_owner.json").exists()
    rows = (
        superuser_db.execute(
            select(MainAITaskWorktree).where(
                MainAITaskWorktree.owner_id == owner.id,
                MainAITaskWorktree.path == str(repo.resolve()),
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
