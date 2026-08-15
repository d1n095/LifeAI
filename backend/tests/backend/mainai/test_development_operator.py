import hashlib
import json
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_operator.service import (
    COMMAND_PROFILES,
    DEVELOPMENT_CAPABILITIES,
    OperatorAuthorizationError,
    OperatorCommandError,
    OperatorContext,
    OperatorPathError,
    OperatorVerificationRequired,
    capability_missing,
    checkpoint_operator_progress,
    commit_changes,
    inspect_repository,
    read_file,
    redact,
    run_profile,
    search_text,
    stage_scoped_changes,
    push_branch,
    write_file,
)
from app.intelligence_governance import record_execution
from app.mainai_execution.checkpoint import record_checkpoint
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree
from app.models.user import User
from app.models.work_intelligence import WorkTraceEvent
from app.work_intelligence import bind_strategy_execution, create_strategy


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _foundation(db, tmp_path):
    owner = User(
        email=f"operator-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    goal = MainAIGoal(
        owner_id=owner.id,
        title="operator",
        original_instruction="safe repository work",
        created_by="test",
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
        idempotency_key="operator-execution",
    )
    strategy = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key="governed-operator",
        version=1,
        work_category="repository_work",
        idempotency_key="operator-strategy",
    )
    binding = bind_strategy_execution(
        db,
        owner_id=owner.id,
        strategy_id=strategy.id,
        execution_id=execution.id,
        idempotency_key="operator-binding",
    )
    marker_token = "a" * 32
    marker = {
        "task_id": str(task.id),
        "job_id": str(job.id),
        "marker_token": marker_token,
    }
    (repo / ".mainai_worktree_owner.json").write_text(json.dumps(marker))
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
        allowed_paths=("safe.txt", "new.txt", "linked.txt", "test_operator_sample.py"),
    )
    return owner, goal, task, job, worktree, context


def test_structured_read_search_and_trace_are_provider_independent(
    superuser_db, tmp_path, monkeypatch
):
    import app.providers.registry as provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider resolution is forbidden")
        ),
    )
    _, _, task, _, _, context = _foundation(superuser_db, tmp_path)
    head = inspect_repository(
        superuser_db, context, "repo_head", idempotency_key="head"
    )
    search = search_text(
        superuser_db,
        context,
        query="canonical",
        scope="safe.txt",
        idempotency_key="search",
    )
    read = read_file(superuser_db, context, path="safe.txt", idempotency_key="read")
    assert head.detail["output"] == context.expected_base_sha
    assert len(search.detail["matches"]) == 1
    assert read.detail["content"] == "canonical\n"
    assert task.status.value != "verified"
    traces = (
        superuser_db.execute(
            select(WorkTraceEvent).order_by(WorkTraceEvent.sequence_number)
        )
        .scalars()
        .all()
    )
    assert [event.action_type for event in traces] == [
        "git_history_inspected",
        "symbol_searched",
        "file_read",
    ]
    assert all(event.provenance["job_id"] == str(context.job_id) for event in traces)


def test_paths_shell_environment_and_secrets_fail_closed(superuser_db, tmp_path):
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    with pytest.raises(OperatorPathError):
        read_file(superuser_db, context, path="../outside", idempotency_key="outside")
    with pytest.raises(OperatorPathError):
        read_file(superuser_db, context, path=".env", idempotency_key="env-file")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (context.repository_root / "linked.txt").symlink_to(outside)
    with pytest.raises(OperatorPathError):
        read_file(superuser_db, context, path="linked.txt", idempotency_key="symlink")
    with pytest.raises(OperatorCommandError):
        run_profile(
            superuser_db,
            context,
            profile_name="shell",
            arguments=["rm", "-rf"],
            idempotency_key="shell",
        )
    with pytest.raises(OperatorCommandError):
        run_profile(
            superuser_db,
            context,
            profile_name="git_diff_check",
            arguments=[],
            environment={"API_KEY": "do-not-leak"},
            idempotency_key="dangerous-env",
        )
    with pytest.raises(OperatorAuthorizationError):
        run_profile(
            superuser_db,
            context,
            profile_name="alembic",
            arguments=["upgrade", "head"],
            idempotency_key="production-db-denied",
        )
    assert not {"merge", "deploy", "shell"} & DEVELOPMENT_CAPABILITIES.keys()
    assert "supersecret" not in redact("API_KEY=supersecret bearer raw-token")
    assert "ghp_" not in redact("ghp_abcdefghijklmnopqrstuvwxyz123456")


def test_edit_is_fenced_hash_checked_and_replay_safe(superuser_db, tmp_path):
    _, _, _, job, _, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    result = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="changed\n",
        expected_sha256=before,
        idempotency_key="write-1",
    )
    replay = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="changed\n",
        expected_sha256=before,
        idempotency_key="write-1",
    )
    assert replay.trace_event_id == result.trace_event_id
    with pytest.raises(OperatorAuthorizationError):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content="different\n",
            expected_sha256=before,
            idempotency_key="write-1",
        )
    job.lease_generation += 1
    superuser_db.flush()
    with pytest.raises(OperatorAuthorizationError):
        write_file(
            superuser_db,
            context,
            path="new.txt",
            content="new\n",
            expected_sha256=None,
            idempotency_key="stale-worker",
        )


def test_allowlisted_test_checkpoint_timeout_and_capability_gap(
    superuser_db, tmp_path, monkeypatch
):
    _, _, task, _, _, context = _foundation(superuser_db, tmp_path)
    (context.repository_root / "test_operator_sample.py").write_text(
        "def test_sample():\n    assert True\n"
    )
    test_result = run_profile(
        superuser_db,
        context,
        profile_name="focused_pytest",
        arguments=["test_operator_sample.py"],
        idempotency_key="focused-test",
    )
    assert test_result.result == "succeeded"
    monkeypatch.setitem(
        COMMAND_PROFILES,
        "focused_pytest",
        replace(COMMAND_PROFILES["focused_pytest"], timeout_seconds=0),
    )
    timed_out = run_profile(
        superuser_db,
        context,
        profile_name="focused_pytest",
        arguments=["test_operator_sample.py"],
        idempotency_key="focused-timeout",
    )
    assert timed_out.result == "timeout"
    checkpoint = checkpoint_operator_progress(
        superuser_db,
        context,
        completed_action_ids=[test_result.trace_event_id],
        next_phase="verification",
        unresolved_failures=[],
    )
    assert checkpoint.executor_state["head_sha"] == context.expected_base_sha
    assert task.status.value != "verified"
    missing = capability_missing(
        superuser_db,
        context,
        capability="browser_automation",
        why_needed="inspect rendered UI",
        unrelated_work_can_continue=True,
        idempotency_key="missing-browser",
    )
    assert missing.result == "capability_missing"
    assert missing.detail["unrelated_work_can_continue"] is True


@pytest.mark.asyncio
async def test_commit_requires_verification_and_scoped_staging(superuser_db, tmp_path):
    _, goal, task, _, worktree, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="verified change\n",
        expected_sha256=before,
        idempotency_key="write-for-commit",
    )
    stage_scoped_changes(
        superuser_db, context, paths=["safe.txt"], idempotency_key="stage"
    )
    with pytest.raises(OperatorVerificationRequired):
        commit_changes(
            superuser_db, context, message="Safe change", idempotency_key="commit"
        )
    record_checkpoint(
        superuser_db,
        task=task,
        goal=goal,
        job_id=context.job_id,
        step="verification",
        data={"verification": {"passed": True}},
    )
    committed = commit_changes(
        superuser_db, context, message="Safe change", idempotency_key="commit"
    )
    assert committed.detail["commit_sha"] == _git(
        context.repository_root, "rev-parse", "HEAD"
    )
    assert worktree.current_commit == committed.detail["commit_sha"]
    assert committed.detail["commit_sha"] != context.expected_base_sha
    with pytest.raises(OperatorAuthorizationError):
        await push_branch(superuser_db, context, idempotency_key="push-without-gate")
    with pytest.raises(OperatorAuthorizationError):
        # A distinct owner cannot reuse this owner-bound task/job/worktree context.
        commit_changes(
            superuser_db,
            replace(context, owner_id=uuid.uuid4()),
            message="No cross-owner",
            idempotency_key="cross-owner",
        )
