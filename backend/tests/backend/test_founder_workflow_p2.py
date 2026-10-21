"""Founder-workflow P2: Claude correction-gate remediations against frozen #294.

Equivalent executable cases for R1–R6, D4/D5 and N1/N4. Does not claim to have
run Claude's local package. No Recall, paid providers, merge, or first-cycle GO.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent_orchestration import create_agent_task, dispatch_task, get_task
from app.bootstrap import bootstrap_founder_user
from app.founder import FOUNDER_USER_ID
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.limiter import rate_limit_key, trusted_client_ip
from app.mainai_execution import executor
from app.mainai_execution.checkpoint import record_checkpoint
from app.mainai_execution.execution_job import (
    _backend_root,
    _finalize_task_outcome,
    _hydrate_isolated_workspace_for_verification,
    _reusable_work_result_checkpoint,
    _verification_cwd_for_work_result,
)
from app.mainai_execution.first_cycle_stop import latest_failure_evidence, should_suppress_paid_replan
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.communication import apply_communication_policy, contradiction_notices
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import classify_agent_task, classify_mainai_task
from app.mainai_verification_registry.service import record_verification_attestation
from app.models.agent_task import AgentTaskStatus
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.providers.base import ChatResult, ProviderError
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import process_claimed_mainai_job
from sqlalchemy import text as sa_text

SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    return user.id


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _goal_and_task(db, owner_id, *, task_type="repo_edit", description="edit one file"):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="p2 goal",
        original_instruction="do the work",
        created_by="test",
    )
    create_plan(
        db,
        goal=goal,
        rationale="one",
        tasks=[
            PlannedTaskSpec(
                description=description,
                task_type=task_type,
                target_files=["docs/p1.md"] if task_type == "repo_edit" else [],
            )
        ],
        created_by="test",
    )
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    db.commit()
    return goal, task


class _Req:
    def __init__(self, path: str, host: str, headers: dict | None = None, user_id=None):
        self.url = SimpleNamespace(path=path)
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}
        self.state = SimpleNamespace(user_id=user_id)


def test_px9_client_xff_cannot_spoof_rate_limit_identity():
    forged = _Req("/api/auth/login", "203.0.113.10", {"x-forwarded-for": "198.51.100.1"})
    assert trusted_client_ip(forged) == "203.0.113.10"
    assert rate_limit_key(forged) == "ip:203.0.113.10:auth.login"
    assert "198.51.100.1" not in rate_limit_key(forged)


def test_px10_one_founder_surface_does_not_lock_another():
    user = uuid.uuid4()
    chat = _Req("/api/chat", "127.0.0.1", user_id=user)
    ops = _Req("/api/mainai/founder-ops/snapshot", "127.0.0.1", user_id=user)
    assert rate_limit_key(chat) != rate_limit_key(ops)
    assert rate_limit_key(chat).startswith(f"user:{user}:")
    assert rate_limit_key(ops).startswith(f"user:{user}:")


def test_px11_one_client_does_not_lock_another():
    a = _Req("/api/auth/login", "203.0.113.10")
    b = _Req("/api/auth/login", "203.0.113.20")
    assert rate_limit_key(a) != rate_limit_key(b)
    proxy_a = _Req("/api/auth/login", "127.0.0.1", {"x-mainai-client-ip": "203.0.113.10"})
    proxy_b = _Req("/api/auth/login", "127.0.0.1", {"x-mainai-client-ip": "203.0.113.20"})
    assert trusted_client_ip(proxy_a) == "203.0.113.10"
    assert trusted_client_ip(proxy_b) == "203.0.113.20"
    assert rate_limit_key(proxy_a) != rate_limit_key(proxy_b)


def test_receipt_wrong_sha_is_not_independent_verification(db_session, superuser_db):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    _goal, task = _goal_and_task(db_session, FOUNDER_USER_ID, task_type="read_only_audit")
    task.status = MainAITaskStatus.completed
    task.completed_at = datetime.utcnow()
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=FOUNDER_USER_ID,
            event_type=MainAITaskEventType.completed,
            detail={"work_result": {"commit_sha": SHA}},
        )
    )
    db_session.commit()
    other = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    record_verification_attestation(
        superuser_db,
        component_id=f"mainai_task:{task.id}",
        candidate_sha=other,
        builder_identity="cursor-builder",
        examiner_identity="claude-examiner",
        review_result="PASS",
        evidence_summary="PASS for a different commit must not verify this task.",
        verification_scope={"task_id": str(task.id)},
        owner_id=FOUNDER_USER_ID,
    )
    superuser_db.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(task.id))
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False


def test_later_fail_revokes_independent_verification(db_session, superuser_db):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    _goal, task = _goal_and_task(db_session, FOUNDER_USER_ID, task_type="read_only_audit")
    task.status = MainAITaskStatus.completed
    task.completed_at = datetime.utcnow()
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=FOUNDER_USER_ID,
            event_type=MainAITaskEventType.completed,
            detail={"work_result": {"commit_sha": SHA}},
        )
    )
    db_session.commit()
    passed = record_verification_attestation(
        superuser_db,
        component_id=f"mainai_task:{task.id}",
        candidate_sha=SHA,
        builder_identity="cursor-builder",
        examiner_identity="claude-examiner",
        review_result="PASS",
        evidence_summary="Independent exact-SHA review of the completed task.",
        verification_scope={"task_id": str(task.id)},
        owner_id=FOUNDER_USER_ID,
    )
    superuser_db.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(task.id))
    assert item["founder_status"] == "independently_verified"

    record_verification_attestation(
        superuser_db,
        component_id=f"mainai_task:{task.id}",
        candidate_sha=SHA,
        builder_identity="cursor-builder",
        examiner_identity="claude-examiner",
        review_result="FAIL",
        evidence_summary="Superseding FAIL revokes the earlier PASS.",
        verification_scope={"task_id": str(task.id)},
        owner_id=FOUNDER_USER_ID,
        invalidates_verification_id=passed.id,
    )
    superuser_db.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(task.id))
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False


@pytest.mark.asyncio
async def test_failed_paid_dispatch_is_recoverable_from_dispatched(db_session, monkeypatch):
    calls = []

    async def _boom(self, messages, model, **kwargs):
        calls.append("fail")
        raise ProviderError("provider down", category="unreachable")

    monkeypatch.setattr(OpenAIProvider, "chat", _boom)
    task = create_agent_task(
        db_session,
        title="recoverable",
        description="retry after 503",
        acceptance_criteria="green",
        created_by="test",
    )
    with pytest.raises(ProviderError):
        await dispatch_task(db_session, task.id, dispatched_by="test")
    refreshed = get_task(db_session, task.id)
    assert refreshed.status == AgentTaskStatus.created

    async def _ok(self, messages, model, **kwargs):
        calls.append("ok")
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _ok)
    event = await dispatch_task(db_session, task.id, dispatched_by="test")
    assert event is not None
    assert get_task(db_session, task.id).status == AgentTaskStatus.result_recorded
    assert calls == ["fail", "ok"]


@pytest.mark.asyncio
async def test_founder_cancel_survives_worker_crash(db_session, superuser_db, owner_id, monkeypatch):
    goal, task = _goal_and_task(db_session, owner_id, task_type="read_only_audit")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    job_id, claimed_owner, generation = claim_next_mainai_job(superuser_db, "worker-p2-cancel", 120)
    assert job_id == job.id
    assert claimed_owner == owner_id
    claimed = superuser_db.get(MainAIJob, job_id)
    assert claimed is not None
    claimed.cancel_requested = True
    superuser_db.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("crash after founder cancel")

    monkeypatch.setattr("app.worker.run_task_execution_job", _boom)
    await process_claimed_mainai_job(
        db_session,
        job_id,
        owner_id,
        worker_id="worker-p2-cancel",
        lease_generation=generation,
        lease_seconds=120,
    )
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    task = db_session.get(MainAITask, task.id)
    assert task is not None and task.status == MainAITaskStatus.cancelled
    item = classify_mainai_task(task)
    assert item["founder_status"] == "cancelled"


@pytest.mark.asyncio
async def test_ambiguous_publication_reuses_work_and_does_not_repay(db_session, owner_id, monkeypatch):
    goal, task = _goal_and_task(db_session, owner_id)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    record_checkpoint(
        db_session,
        task=task,
        goal=goal,
        job_id=job.id,
        step="work_result",
        data={
            "work_result": {
                "worktree_id": "wt-1",
                "worktree_path": "/tmp/wt-1",
                "commit_sha": SHA,
                "files": ["docs/p1.md"],
            }
        },
    )
    record_checkpoint(
        db_session,
        task=task,
        goal=goal,
        job_id=job.id,
        step="publication_attempt",
        data={"publication_identity": "wt-1"},
    )
    db_session.commit()
    _finalize_task_outcome(
        db_session,
        task,
        passed=False,
        evidence={
            "error": "unexpected_worker_crash",
            "origin": "unknown",
            "first_cycle_state": "external_boundary_failed",
            "reconcilable": True,
            "publication_identity": "wt-1",
        },
    )
    db_session.commit()
    db_session.refresh(task)
    evidence = latest_failure_evidence(db_session, task)
    assert evidence is not None
    assert evidence.get("publication_identity") == "wt-1"
    assert evidence.get("reconcilable") is True
    assert should_suppress_paid_replan(task, evidence=evidence) is True

    paid: list[int] = []

    async def _forbidden(*args, **kwargs):
        paid.append(1)
        raise AssertionError("second paid run after ambiguous publication")

    monkeypatch.setattr("app.mainai_execution.execution_job.billed_execution_chat", _forbidden)
    later_job = uuid.uuid4()
    reused = _reusable_work_result_checkpoint(db_session, task_id=task.id, job_id=later_job)
    assert reused is not None
    assert reused.executor_state["work_result"]["worktree_id"] == "wt-1"
    assert paid == []


def test_local_verification_retry_does_not_reuse_unrelated_work_result(db_session, owner_id):
    goal, task = _goal_and_task(db_session, owner_id)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    record_checkpoint(
        db_session,
        task=task,
        goal=goal,
        job_id=job.id,
        step="work_result",
        data={"work_result": {"worktree_id": "local-fail", "files": ["docs/p1.md"]}},
    )
    db_session.commit()
    later_job = uuid.uuid4()
    assert _reusable_work_result_checkpoint(db_session, task_id=task.id, job_id=later_job) is None
    same_job = _reusable_work_result_checkpoint(db_session, task_id=task.id, job_id=job.id)
    assert same_job is not None


def test_isolated_workspace_verification_can_pass(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "backend" / "app").mkdir(parents=True)
    (source / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (source / "backend" / "tests").mkdir()
    (source / "backend" / "tests" / "test_isolated_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    dest = tmp_path / "isolated"
    dest.mkdir()
    (dest / "docs").mkdir()
    (dest / "docs" / "p1.md").write_text("safe", encoding="utf-8")
    monkeypatch.setattr("app.mainai_execution.execution_job._repo_root", lambda: source)
    cwd = _verification_cwd_for_work_result(
        {
            "workspace_root": str(dest),
            "worktree_path": str(dest),
            "isolated_workspace": True,
        }
    )
    assert Path(cwd).resolve() == (dest / "backend").resolve()
    assert Path(cwd).resolve() != _backend_root().resolve()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_isolated_ok.py"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _hydrate_isolated_workspace_for_verification(dest, source=dest) == dest.resolve()


def test_cancelled_work_is_not_a_failed_blocker(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    _goal, task = _goal_and_task(db_session, FOUNDER_USER_ID, task_type="read_only_audit")
    task.status = MainAITaskStatus.cancelled
    task.completed_at = datetime.utcnow()
    db_session.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(task.id))
    assert item["founder_status"] == "cancelled"
    assert snap["work_item_counts"].get("cancelled") == 1
    assert snap["work_item_counts"].get("failed", 0) == 0
    assert snap["blocker_counts"]["operational"] == len(snap["blockers"])
    assert all(b.get("kind") != "failed_work" for b in snap["blockers"])


def test_agent_dispatched_is_acknowledged_not_running():
    item = classify_agent_task(SimpleNamespace(id=uuid.uuid4(), title="x", status=AgentTaskStatus.dispatched))
    assert item["founder_status"] == "acknowledged"
    assert item["founder_status"] != "running"


def test_unavailable_evidence_does_not_fabricate_disagreement():
    notices = contradiction_notices(
        founder_message="All work is independently verified.",
        evidence={"evidence_unavailable": True, "independently_verified": None},
    )
    assert notices == []
    text = apply_communication_policy(
        "Jag kan inte bekräfta det.",
        founder_message="All work is independently verified.",
        evidence={"evidence_unavailable": True},
    )
    assert "Osäker" in text
    assert "Självständig verifiering kräver" not in text
