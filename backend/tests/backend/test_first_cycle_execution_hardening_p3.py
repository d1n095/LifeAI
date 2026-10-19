"""FO-6/FO-7 execution-layer completion: remote-fetch paid-replan, crash-recovery loop,
publication locking, founder diagnostics. Isolated successor of #289. No GO claim.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClientError
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.first_cycle_ops import founder_diagnostics, snapshot_first_cycle
from app.mainai_execution.first_cycle_stop import (
    STATE_EXTERNAL_BOUNDARY_FAILED,
    STATE_STOP_BEFORE_MERGE,
    assert_publication_permitted,
    classify_publication_failure,
    should_suppress_paid_replan,
    stop_first_cycle,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.publication_errors import (
    PublicationForbiddenAfterStopError,
    PublicationNetworkError,
    PublicationTimeoutError,
)
from app.mainai_execution.recovery_classifier import _classify, classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import (
    MAX_AUTO_RECOVERY_TAKEOVERS_PER_TASK,
    TakeoverCapExhaustedError,
    count_completed_takeovers,
    execute_takeover,
)
from app.mainai_execution.worktree import WorktreeError, WorktreeTimeoutError, fetch_remote_branch
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_recovery import MainAIRecoveryStatus, RecoveryClassification
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

FROZEN_289 = "ca60ba54febb49e1c7585fb776811564d54e3d21"
PARENT_290 = "6c988148a7e30531fac21e19c044f5570d95f501"
THIS_BRANCH = "cursor/first-cycle-execution-completion-91c1"
LOCAL_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _goal_task(db, owner_id, **spec_kwargs):
    goal = planner.create_goal(
        db, owner_id=owner_id, title="p3-hardening", original_instruction="local", created_by="founder"
    )
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "repo_edit")
    spec_kwargs.setdefault("target_files", ["backend/tests/test_scratch_edit.py"])
    planner.create_plan(db, goal=goal, rationale="p3", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def test_fetch_missing_remote_branch_is_none_not_a_boundary_error(tmp_path, monkeypatch):
    import subprocess

    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "claude/mainai-task-x"], check=True)
    monkeypatch.setattr("app.mainai_execution.worktree._require_ownership", lambda worktree: None)

    class _Wt:
        path = str(clone)
        branch = "claude/mainai-task-missing"

    assert fetch_remote_branch(_Wt()) is None


def test_fetch_timeout_is_typed_publication_timeout(monkeypatch):
    monkeypatch.setattr("app.mainai_execution.worktree._require_ownership", lambda worktree: None)

    def _boom(*args, **kwargs):
        raise WorktreeTimeoutError("timed out")

    monkeypatch.setattr("app.mainai_execution.worktree._run_git", _boom)

    class _Wt:
        path = "/tmp"
        branch = "claude/mainai-task-x"

    with pytest.raises(PublicationTimeoutError):
        fetch_remote_branch(_Wt())


def test_fetch_origin_failure_is_typed_network_error(monkeypatch):
    monkeypatch.setattr("app.mainai_execution.worktree._require_ownership", lambda worktree: None)

    def _boom(*args, **kwargs):
        raise WorktreeError("git ls-remote failed")

    monkeypatch.setattr("app.mainai_execution.worktree._run_git", _boom)

    class _Wt:
        path = "/tmp"
        branch = "claude/mainai-task-x"

    with pytest.raises(PublicationNetworkError):
        fetch_remote_branch(_Wt())


def test_remote_fetch_failure_classifies_unsafe_not_local_committed():
    assert (
        _classify(
            {
                "remote_fetch_failed": True,
                "remote_fetch_origin": "network",
                "worktree_current_commit": LOCAL_COMMIT,
                "remote_branch_exists": False,
            }
        )
        == RecoveryClassification.unsafe_to_auto_recover
    )


def test_true_missing_remote_branch_still_salvageable_local_commit():
    assert (
        _classify(
            {
                "worktree_current_commit": LOCAL_COMMIT,
                "remote_branch_exists": False,
            }
        )
        == RecoveryClassification.local_committed_not_pushed
    )


def test_unflushed_local_head_ahead_of_base_is_still_salvageable():
    assert (
        _classify(
            {
                "worktree_local_head_sha": LOCAL_COMMIT,
                "worktree_base_sha": BASE_SHA,
                "remote_branch_exists": False,
            }
        )
        == RecoveryClassification.local_committed_not_pushed
    )


def test_finalized_checkpoint_is_never_auto_unpushed_even_if_remote_lookup_misses():
    assert (
        _classify(
            {
                "has_finalized_checkpoint": True,
                "worktree_current_commit": LOCAL_COMMIT,
                "remote_branch_exists": False,
            }
        )
        == RecoveryClassification.pushed_no_pr
    )


def test_remote_fetch_failure_suppresses_paid_replan(db_session, owner_id):
    _, task = _goal_task(db_session, owner_id)
    evidence = {"remote_fetch_failed": True, "remote_fetch_origin": "network", "origin": "network"}
    assert classify_publication_failure(evidence=evidence) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert should_suppress_paid_replan(task, evidence=evidence) is True


@pytest.mark.asyncio
async def test_github_5xx_ref_lookup_does_not_look_like_unpushed_local_commit(
    db_session, owner_id, monkeypatch
):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)

    async def _fail(self, branch: str) -> str:
        raise GitHubClientError("upstream", status_code=502)

    monkeypatch.setattr("app.integrations.github_client.GitHubClient.is_configured", lambda self: True)
    monkeypatch.setattr("app.integrations.github_client.GitHubClient.get_ref", _fail)
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=job, record=record)
    db_session.commit()
    assert record.status == MainAIRecoveryStatus.blocked
    assert record.manual_review_required is True
    assert record.evidence.get("remote_fetch_failed") is True
    assert record.evidence.get("remote_branch_exists") is not False
    assert record.evidence.get("pr_exists") is not False
    classified = classify_recovery_record(db_session, record=record)
    assert classified.status == MainAIRecoveryStatus.blocked
    assert classified.classification is None


@pytest.mark.asyncio
async def test_takeover_cap_blocks_crash_recovery_loop(db_session, superuser_db, owner_id):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    first_job_id = task.mainai_job_id
    claim_next_mainai_job(superuser_db, "dead-worker-1", 120)
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": str(first_job_id)},
    )
    superuser_db.commit()
    await Worker()._advance_mainai_execution_auto_recovery(superuser_db)
    superuser_db.commit()
    superuser_db.expire_all()
    assert count_completed_takeovers(superuser_db, task_id=task.id) >= MAX_AUTO_RECOVERY_TAKEOVERS_PER_TASK
    task = superuser_db.get(MainAITask, task.id)
    second_job_id = task.mainai_job_id
    assert second_job_id != first_job_id
    claim_next_mainai_job(superuser_db, "dead-worker-2", 120)
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": str(second_job_id)},
    )
    superuser_db.commit()
    await Worker()._advance_mainai_execution_auto_recovery(superuser_db)
    superuser_db.commit()
    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.blocked
    assert task.next_retry_at is None
    job_ids = {
        row[0]
        for row in superuser_db.execute(
            sa_text("SELECT id FROM mainai_jobs WHERE owner_id = :oid"),
            {"oid": str(owner_id)},
        ).all()
    }
    assert first_job_id in job_ids
    assert second_job_id in job_ids
    assert len(job_ids) == 2


@pytest.mark.asyncio
async def test_execute_takeover_refuses_after_cap(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="w")
    db_session.commit()
    db_session.refresh(task)
    prior = get_or_create_recovery_record(db_session, task=task, job=job)
    prior.status = MainAIRecoveryStatus.completed
    prior.completed_at = datetime.utcnow()
    prior.classification = RecoveryClassification.nothing_done
    prior.takeover_job_id = job.id
    db_session.commit()
    with pytest.raises(TakeoverCapExhaustedError, match="takeover cap"):
        await execute_takeover(
            db_session,
            task=task,
            goal=goal,
            record=prior,
            dispatched_by="recovery-worker",
        )


def test_publication_lock_refuses_after_stop(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    stop_first_cycle(
        db_session,
        task,
        reason="p3 lock",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"merged": False},
        goal=goal,
    )
    db_session.commit()
    with pytest.raises(PublicationForbiddenAfterStopError):
        assert_publication_permitted(db_session, task)


def test_founder_diagnostics_never_claim_go(db_session, owner_id):
    goal, _task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    diag = founder_diagnostics(db_session, goal)
    snap = snapshot_first_cycle(db_session, goal)
    assert diag["go"] is False
    assert diag["merged"] is False
    assert diag["codex_transport"] == "FAIL/BLOCKED"
    assert snap["founder_diagnostics"]["go"] is False
    blob = json.dumps(diag).lower()
    for secret in ("github_token", "x-access-token", "password", "authorization"):
        assert secret not in blob


def test_staging_records_stay_no_go_on_this_successor():
    root = Path(__file__).resolve().parents[3]
    go_nogo = json.loads((root / "docs" / "first_cycle_staging_go_nogo.json").read_text())
    manifest = json.loads((root / "docs" / "first_cycle_staging_manifest.json").read_text())
    assert go_nogo["go"] is False
    assert manifest.get("go") is False
    assert go_nogo["branch"] == THIS_BRANCH
    assert manifest["branch"] == THIS_BRANCH
    assert go_nogo["parent_289"] == FROZEN_289
    assert go_nogo["parent_290"] == PARENT_290
    assert manifest["successor_of"]["pr"] == 290
    assert manifest["successor_of"]["sha"] == PARENT_290
    assert go_nogo.get("pr") != 289
    assert go_nogo.get("pr") != 288
    assert "certified Path A" not in str(manifest["inputs"]["pr_276"].get("role") or "")
    remaining = " ".join(go_nogo["remaining_codex"])
    for token in ("CRED-1/2/3", "E2E-3", "GRANT-PROD", "TA-12", "CI-6", "#280"):
        assert token in remaining
