"""First-cycle operational readiness P2: inputs, cancel/retry, staging records, local dry run.

Isolated successor of frozen #288. Codex transport/grants stay FAIL/BLOCKED. No GO claim.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import TaskExecutionError, _parse_code_agent_response
from app.mainai_execution.first_cycle_ops import (
    run_founder_supervised_local_dry_run,
    snapshot_first_cycle,
)
from app.mainai_execution.first_cycle_stop import (
    STATE_EXTERNAL_PUSH_REJECTED,
    STATE_STOP_BEFORE_MERGE,
    classify_publication_failure,
    job_has_founder_cancel,
    stop_first_cycle,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record
from app.mainai_execution.recovery_takeover import TakeoverError, execute_takeover
from app.mainai_execution.worktree import WorktreeError, sanitize_git_commit_message
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


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


def _goal_task(db, owner_id, **spec_kwargs) -> tuple:
    goal = planner.create_goal(
        db, owner_id=owner_id, title="ops-p2", original_instruction="local cycle", created_by="founder"
    )
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "repo_edit")
    spec_kwargs.setdefault("target_files", ["backend/tests/test_scratch_edit.py"])
    planner.create_plan(
        db, goal=goal, rationale="ops-p2", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test"
    )
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(
            content=response_text,
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    return _chat


def test_model_filename_control_chars_and_nul_are_rejected():
    with pytest.raises(TaskExecutionError, match="control"):
        _parse_code_agent_response(
            '{"files": [{"path": "backend/app/x.py\\u0000.git/config", "content": "x"}], "commit_message": "x"}'
        )
    with pytest.raises(TaskExecutionError, match="control|whitespace"):
        _parse_code_agent_response(
            '{"files": [{"path": "backend/app/x.py\\n.git/config", "content": "x"}], "commit_message": "x"}'
        )
    with pytest.raises(TaskExecutionError):
        _parse_code_agent_response(
            '{"files": [{"path": ".git/hooks/pre-commit", "content": "x"}], "commit_message": "x"}'
        )
    with pytest.raises(TaskExecutionError):
        _parse_code_agent_response(
            '{"files": [{"path": "stop_before_merge/../../../etc/passwd", "content": "x"}], "commit_message": "x"}'
        )


def test_safe_relative_filename_still_accepted():
    parsed = _parse_code_agent_response(
        '{"files": [{"path": "backend/tests/test_scratch_edit.py", "content": "x = 1\\n"}], "commit_message": "document test"}'
    )
    assert parsed["files"][0]["path"] == "backend/tests/test_scratch_edit.py"


def test_model_commit_message_newlines_and_leading_dash_are_sanitized():
    safe = sanitize_git_commit_message("-F /etc/passwd\nCo-authored-by: attacker")
    assert "\n" not in safe
    assert not safe.startswith("-")
    assert "/etc/passwd" in safe
    with pytest.raises(WorktreeError):
        sanitize_git_commit_message("bad\x00message")
    with pytest.raises(WorktreeError):
        sanitize_git_commit_message("   ")
    with pytest.raises(WorktreeError):
        sanitize_git_commit_message(None)


def test_failure_class_without_origin_cannot_classify_publication_stop():
    assert classify_publication_failure(evidence={"failure_class": "PushRejectedError"}) is None
    assert classify_publication_failure(evidence={"error": "PushRejectedError", "reason": "timed_out"}) is None
    assert (
        classify_publication_failure(
            evidence={"origin": "push", "failure_class": "PushRejectedError"}
        )
        == STATE_EXTERNAL_PUSH_REJECTED
    )
    # A typed origin still fail-closes even if failure_class is a mismatched smuggle.
    assert (
        classify_publication_failure(
            evidence={"origin": "push", "failure_class": "GrantDeniedError"}
        )
        == STATE_EXTERNAL_PUSH_REJECTED
    )


def test_retry_tick_parks_due_task_on_stopped_goal(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    task.next_retry_at = datetime.utcnow() - timedelta(seconds=5)
    db_session.commit()
    stop_first_cycle(
        db_session,
        task,
        reason="ops-p2 stop",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"merged": False},
        goal=goal,
    )
    db_session.commit()

    # Re-arm a sibling-shaped retry: the stopped task is blocked; seed another due retry
    # by restoring retryable_failed as if park missed it (the remaining race).
    task.status = MainAITaskStatus.retryable_failed
    task.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    task.completed_at = None
    db_session.commit()

    Worker()._advance_mainai_execution_retries(db_session)
    db_session.commit()
    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.blocked
    assert task.next_retry_at is None


@pytest.mark.asyncio
async def test_takeover_refuses_founder_cancelled_dead_job(db_session, superuser_db, owner_id):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    claim_next_mainai_job(superuser_db, "dead-worker", 120)
    superuser_db.execute(
        sa_text(
            "UPDATE mainai_jobs SET cancel_requested = true, lease_expires_at = now() - interval '1 second' WHERE id = :id"
        ),
        {"id": str(task.mainai_job_id)},
    )
    superuser_db.commit()
    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    job = db_session.get(MainAIJob, task.mainai_job_id)
    assert job_has_founder_cancel(job)
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    with pytest.raises(TakeoverError, match="cancel"):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")


@pytest.mark.asyncio
async def test_auto_recovery_finalizes_cancelled_dead_job_without_takeover(
    db_session, superuser_db, owner_id
):
    goal, task = _goal_task(db_session, owner_id, task_type="read_only_audit", target_files=[])
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    claim_next_mainai_job(superuser_db, "dead-worker", 120)
    superuser_db.execute(
        sa_text(
            "UPDATE mainai_jobs SET cancel_requested = true, lease_expires_at = now() - interval '1 second' WHERE id = :id"
        ),
        {"id": str(task.mainai_job_id)},
    )
    superuser_db.commit()
    await Worker()._advance_mainai_execution_auto_recovery(superuser_db)
    superuser_db.commit()
    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    job = superuser_db.get(MainAIJob, task.mainai_job_id)
    assert task.status == MainAITaskStatus.cancelled
    assert job.status == MainAIJobStatus.cancelled
    assert task.next_retry_at is None


def test_staging_records_do_not_claim_go_or_certified_path_a():
    root = Path(__file__).resolve().parents[3]
    go_nogo = json.loads((root / "docs" / "first_cycle_staging_go_nogo.json").read_text())
    manifest = json.loads((root / "docs" / "first_cycle_staging_manifest.json").read_text())
    assert go_nogo["go"] is False
    assert go_nogo["parent_288"] == "1ea41d7db424cba6b4d9ec93da79fa85d809d2fb"
    assert manifest.get("go") is False
    pr276 = manifest["inputs"]["pr_276"]
    role = str(pr276.get("role") or "")
    assert "not a first-cycle go certification" in role.lower()
    assert "certified Path A" not in role


@pytest.mark.asyncio
async def test_founder_supervised_first_cycle_dry_run_stops_before_merge(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat(
            '{"files": [{"path": "backend/tests/test_scratch_edit.py", '
            '"content": "def test_scratch_edit():\\n    assert 2 + 3 == 5\\n"}], '
            '"commit_message": "Document isolated first-cycle edit"}'
        ),
    )
    result = await run_founder_supervised_local_dry_run(
        db_session, owner_id=owner_id, claim_db=superuser_db, repo_root=tmp_path
    )
    assert result["stages"] == [
        "goal",
        "clear_state",
        "proposed_task",
        "approval_required",
        "founder_approval",
        "dispatch",
        "local_verification",
        "STOP",
    ]
    assert result["stopped"] is True
    assert result["merged"] is False
    assert result["go"] is False
    assert len(result["verification_sha"]) == 40
    empty = result["empty_snapshot"]
    assert empty["blockers"] == [] or isinstance(empty["blockers"], list)
    assert empty["costs"]["live_paid_calls"] is False
    final = result["final_snapshot"]
    assert final["stopped"] is True
    assert final["merged"] is False
    assert final["go"] is False
    assert final["first_cycle_state"] == STATE_STOP_BEFORE_MERGE
    assert any(b.get("code") == "first_cycle_stop" for b in final["blockers"])
    assert "merge" not in final["authorized_actions"]
    stages = {row["stage"] for row in final["audit"]}
    assert {"proposed_task", "founder_approval", "local_verification", "STOP"} <= stages
    assert all(row["evidence_sha"] for row in final["audit"])
    goal_row = superuser_db.execute(
        sa_text("SELECT status, final_outcome FROM mainai_goals WHERE id = :id"),
        {"id": result["goal_id"]},
    ).one()
    assert goal_row[0] == MainAIGoalStatus.blocked.value
    assert '"merged": false' in goal_row[1].replace(" ", "") or '"merged":false' in goal_row[1].replace(" ", "")
