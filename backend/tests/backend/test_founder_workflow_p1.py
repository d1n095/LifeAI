"""Founder-workflow P1: FO-6, D1–D5, communication, proxy-adjacent auth, safe mode, authority.

Does not enable Recall, GitHub writes, activation mode, or first-cycle GO.
Codex transport/grants stay unimplemented.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.agent_orchestration import create_agent_task, dispatch_task, get_task
from app.bootstrap import bootstrap_founder_user
from app.config import get_settings
from app.db import SessionLocal
from app.founder import FOUNDER_USER_ID
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor
from app.mainai_execution.execution_job import (
    _live_server_checkout,
    _proposal_write_root,
    _propose_repo_edit,
)
from app.mainai_execution.first_cycle_stop import (
    STATE_EXTERNAL_PUSH_REJECTED,
    classify_publication_failure,
    latest_failure_evidence,
    should_suppress_paid_replan,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_execution.publication_errors import PushRejectedError, map_publication_boundary_error
from app.mainai_execution.replan import ReplanSuppressedError, find_replan_trigger, trigger_replan
from app.mainai_execution.worktree import WorktreePushRejected
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import classify_mainai_task, is_independent_exact_sha_receipt
from app.mainai_verification_registry.service import record_verification_attestation
from app.models.agent_task import AgentTaskStatus
from app.models.mainai_execution import MainAITask, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import process_claimed_mainai_job
from app.workforce.broker import DelegationBrokerError, submit_delegation_request
from app.workforce.registry import register_workforce_agent

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
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


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _goal_and_task(db, owner_id, *, task_type="repo_edit", description="edit one file"):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="p1 goal",
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


def test_worktree_push_rejected_maps_to_typed_push_error():
    mapped = map_publication_boundary_error(WorktreePushRejected("WorktreePushRejected"))
    assert isinstance(mapped, PushRejectedError)
    assert mapped.origin == "push"
    assert mapped.first_cycle_state == STATE_EXTERNAL_PUSH_REJECTED
    assert classify_publication_failure(mapped, evidence=mapped.evidence()) == STATE_EXTERNAL_PUSH_REJECTED


def test_stored_push_rejection_evidence_suppresses_paid_replan(db_session, owner_id):
    goal, task = _goal_and_task(db_session, owner_id)
    mapped = map_publication_boundary_error(WorktreePushRejected("WorktreePushRejected"))
    from app.mainai_execution.execution_job import _finalize_task_outcome

    _finalize_task_outcome(db_session, task, passed=False, evidence=mapped.evidence(), cause=mapped)
    db_session.commit()
    db_session.refresh(task)
    evidence = latest_failure_evidence(db_session, task)
    assert evidence is not None
    assert evidence.get("origin") == "push"
    assert evidence.get("first_cycle_state") == STATE_EXTERNAL_PUSH_REJECTED
    assert should_suppress_paid_replan(task, evidence=evidence) is True
    assert find_replan_trigger(db_session, goal=goal) is None


@pytest.mark.asyncio
async def test_governed_goal_recovery_does_not_replan_after_push_reject(db_session, owner_id, monkeypatch):
    goal, task = _goal_and_task(db_session, owner_id)
    mapped = map_publication_boundary_error(WorktreePushRejected("WorktreePushRejected"))
    from app.mainai_execution.execution_job import _finalize_task_outcome

    _finalize_task_outcome(db_session, task, passed=False, evidence=mapped.evidence(), cause=mapped)
    db_session.commit()

    paid = []

    async def _forbidden(*args, **kwargs):
        paid.append(1)
        raise AssertionError("paid replan after FO-6 push rejection")

    monkeypatch.setattr("app.mainai_execution.replan.propose_plan_via_ai", _forbidden)
    with pytest.raises(ReplanSuppressedError):
        await trigger_replan(db_session, goal=goal, failed_task=task, dispatched_by="test")
    assert paid == []
    assert find_replan_trigger(db_session, goal=goal) is None


@pytest.mark.asyncio
async def test_worker_crash_finalizes_running_task_execution(db_session, superuser_db, owner_id, monkeypatch):
    goal, task = _goal_and_task(db_session, owner_id, task_type="read_only_audit")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.running
    job_id, claimed_owner, generation = claim_next_mainai_job(superuser_db, "worker-p1", 120)
    assert job_id == job.id
    assert claimed_owner == owner_id

    async def _boom(*args, **kwargs):
        raise RuntimeError("ambiguous push crash")

    monkeypatch.setattr("app.worker.run_task_execution_job", _boom)
    await process_claimed_mainai_job(
        db_session,
        job_id,
        owner_id,
        worker_id="worker-p1",
        lease_generation=generation,
        lease_seconds=120,
    )
    db_session.expire_all()
    superuser_db.expire_all()
    _set_rls(db_session, owner_id)
    task = db_session.get(MainAITask, task.id)
    job = superuser_db.get(MainAIJob, job_id)
    assert task is not None and task.status == MainAITaskStatus.failed
    assert job is not None and job.status == MainAIJobStatus.failed
    evidence = latest_failure_evidence(db_session, task)
    assert evidence is not None
    assert evidence.get("error") == "unexpected_worker_crash"


def test_checkpoint_and_self_event_are_not_independent_verification():
    task = SimpleNamespace(
        id=uuid.uuid4(),
        goal_id=uuid.uuid4(),
        status=MainAITaskStatus.completed,
        blocker_reason=None,
        mainai_job_id=None,
        description="builder finished",
        approval_required=False,
    )
    event = SimpleNamespace(event_type=MainAITaskEventType.verification_passed)
    item = classify_mainai_task(
        task,
        events=[event],
        checkpoint_test_status="passed",
        checkpoint_ci_status="success",
    )
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False
    assert is_independent_exact_sha_receipt({"candidate_sha": SHA, "builder_identity": "cursor", "examiner_identity": "cursor", "review_result": "PASS"}) is False
    assert is_independent_exact_sha_receipt({"candidate_sha": "short", "builder_identity": "cursor", "examiner_identity": "claude", "review_result": "PASS"}) is False


def test_independent_exact_sha_receipt_is_required(db_session, superuser_db):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    goal, task = _goal_and_task(db_session, FOUNDER_USER_ID, task_type="read_only_audit")
    task.status = MainAITaskStatus.completed
    task.completed_at = datetime.utcnow()
    db_session.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(task.id))
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False

    record_verification_attestation(
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
    assert item["independent_receipt_sha"] == SHA
    assert item["independent_examiner"] == "claude-examiner"


def test_task_and_job_are_not_double_counted(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    goal, task = _goal_and_task(db_session, FOUNDER_USER_ID, task_type="read_only_audit")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    task_items = [i for i in snap["work_items"] if i.get("task_id") == str(task.id)]
    job_items = [i for i in snap["work_items"] if i.get("job_id") == str(job.id)]
    assert len(task_items) == 1
    assert job_items == []
    assert sum(snap["work_item_counts"].values()) == len(snap["work_items"])
    failed_blockers = [b for b in snap["blockers"] if b.get("kind") == "failed_work" and b.get("id") == str(job.id)]
    assert failed_blockers == []


def test_authority_is_explainable_and_go_stays_false(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    assert snap["first_cycle_go"] is False
    authority = snap["authority"]
    assert authority["first_cycle_go"] is False
    assert authority["policy_transparent"] is True
    assert authority["sensitive_changes_require_founder_approval"] is True
    assert authority["independent_security_enforcement"] is True
    assert authority["live_checkout_writes_authorized"] is False
    codes = {item["code"] for item in authority["restrictions"]}
    assert "github_write_disabled" in codes
    assert "live_checkout_writes_disabled" in codes
    assert "recall_http_unmounted" in codes
    for item in authority["restrictions"]:
        assert item["explain"]


@pytest.mark.asyncio
async def test_concurrent_dispatch_makes_one_paid_call(db_session, monkeypatch):
    paid = []
    started = threading.Event()
    release = threading.Event()

    async def _chat(self, messages, model, **kwargs):
        paid.append(1)
        started.set()
        await asyncio.sleep(0.3)
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    task = create_agent_task(
        db_session,
        title="concurrent",
        description="one paid call",
        acceptance_criteria="green",
        created_by="test",
    )
    db_session.commit()
    task_id = task.id

    def _run():
        session = SessionLocal()
        try:
            current_user_id_var.set(str(FOUNDER_USER_ID))
            session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(FOUNDER_USER_ID)})
            return asyncio.run(dispatch_task(session, task_id, dispatched_by="racer"))
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_run)
        second = pool.submit(_run)
        results = [first.result(timeout=10), second.result(timeout=10)]

    from app.mainai_founder_ops.assignment_guard import DuplicateWorkError

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, DuplicateWorkError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert len(paid) == 1
    db_session.expire_all()
    refreshed = get_task(db_session, task_id)
    assert refreshed.status in {AgentTaskStatus.result_recorded, AgentTaskStatus.dispatched}


def test_concurrent_http_dispatch_is_atomic(client, monkeypatch):
    paid = []
    gate = threading.Event()

    async def _chat(self, messages, model, **kwargs):
        paid.append(threading.current_thread().name)
        gate.wait(timeout=2)
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    csrf = _login(client)
    created = client.post(
        "/api/admin/agents/tasks",
        json={
            "title": "http race",
            "description": "one dispatch",
            "acceptance_criteria": "green",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    statuses: list[int] = []

    def _post():
        res = client.post(f"/api/admin/agents/tasks/{task_id}/dispatch", headers={"X-CSRF-Token": csrf})
        statuses.append(res.status_code)
        return res.status_code

    t1 = threading.Thread(target=_post)
    t2 = threading.Thread(target=_post)
    t1.start()
    t2.start()
    threading.Event().wait(0.2)
    gate.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert statuses.count(200) == 1
    assert 409 in statuses
    assert len(paid) == 1


def test_concurrent_delegation_inserts_one_request(superuser_db):
    owner = User(email=f"p1-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="local-classifier",
        name="Local Classifier",
        role="specialist",
        agent_type="LOCAL_MODEL",
        provider_type="local",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    goal = create_goal(superuser_db, owner_id=owner.id, title="d3", original_instruction="x", created_by="test")
    create_plan(
        superuser_db,
        goal=goal,
        rationale="one",
        tasks=[PlannedTaskSpec(description="the work", task_type="read_only_audit")],
        created_by="test",
    )
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    superuser_db.commit()
    task_id = task.id
    owner_id = owner.id
    results: list[object] = []

    def _submit():
        session = SessionLocal()
        try:
            current_user_id_var.set(str(owner_id))
            session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
            row = submit_delegation_request(
                session,
                owner_id=owner_id,
                goal_text="classify",
                required_capability="low_risk_classification",
                task_id=task_id,
                verification_requirement="independent_verifier",
            )
            session.commit()
            results.append(row.id)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=_submit)
    t2 = threading.Thread(target=_submit)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    ids = [r for r in results if isinstance(r, uuid.UUID)]
    errors = [r for r in results if isinstance(r, DelegationBrokerError)]
    assert len(ids) == 1
    assert len(errors) == 1


def test_stop_observability_failure_is_http_503(client, monkeypatch):
    _login(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("epoch missing")

    monkeypatch.setattr("app.routers.mainai_founder_ops.query_stop_status", _boom)
    res = client.get("/api/mainai/founder-ops/stop")
    assert res.status_code == 503
    assert "blockerad" in res.json()["detail"].lower()


def test_snapshot_stop_unknown_is_blocked(db_session, monkeypatch):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)

    def _boom(*args, **kwargs):
        raise RuntimeError("epoch missing")

    monkeypatch.setattr("app.mainai_founder_ops.snapshot.query_stop_status", _boom)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    assert snap["stop"]["blocked"] is True
    assert snap["stop"]["observability_failed"] is True
    assert snap["health"]["backend_reachable"] is True


def test_live_checkout_is_not_written_without_authorization(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_allow_live_checkout_writes", False)
    live = _live_server_checkout()
    sentinel = live / f".p1-live-checkout-{uuid.uuid4()}.txt"
    task = SimpleNamespace(id=uuid.uuid4())
    root = _proposal_write_root(task)
    assert root != live
    assert root.exists()
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_propose_repo_edit_writes_isolated_workspace(db_session, owner_id, monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_allow_live_checkout_writes", False)
    goal, task = _goal_and_task(db_session, owner_id)
    live = _live_server_checkout()
    marker_name = f"p1-safe-{task.id}.md"
    live_marker = live / marker_name

    async def _chat(*args, **kwargs):
        return (
            SimpleNamespace(
                content='{"files": [{"path": "%s", "content": "unsafe"}], "commit_message": "x"}' % marker_name,
                provider="openai",
                model="test",
            ),
            ["openai"],
        )

    monkeypatch.setattr("app.mainai_execution.execution_job.billed_execution_chat", _chat)
    monkeypatch.setattr(
        "app.mainai_execution.execution_job.prepare_code_agent_prompt",
        lambda *args, **kwargs: SimpleNamespace(text="prompt", explicit_paths=[marker_name], fully_shown_paths=[marker_name]),
    )
    monkeypatch.setattr("app.mainai_execution.execution_job._envelope_for_path_a_task", lambda db, task: None)
    monkeypatch.setattr("app.mainai_execution.execution_job._explicit_edit_targets", lambda db, task: [marker_name])
    monkeypatch.setattr("app.mainai_execution.execution_job._runtime_edit_targets", lambda *args, **kwargs: [marker_name])
    monkeypatch.setattr("app.mainai_execution.execution_job._assert_repo_edit_writes_allowed", lambda *args, **kwargs: None)
    result = await _propose_repo_edit(db_session, task)
    assert result["live_checkout_mutated"] is False
    assert result["isolated_workspace"] is True
    assert not live_marker.exists()
    written = Path(result["workspace_root"]) / marker_name
    assert written.exists()
    written.unlink(missing_ok=True)


def test_chat_uses_live_snapshot_evidence(client, monkeypatch):
    dim = get_settings().embedding_dim

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * dim for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content="Jo, Recall är aktiv.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    csrf = _login(client)
    sent = client.post(
        "/api/chat",
        json={"message": "Recall är monterad?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert sent.status_code == 200, sent.text
    reply = sent.json()["reply"]
    assert "Personal Recall" in reply
    assert "GitHub-skrivning" not in reply


def test_founder_ops_http_exposes_authority(client):
    _login(client)
    res = client.get("/api/mainai/founder-ops/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["first_cycle_go"] is False
    assert body["authority"]["live_checkout_writes_authorized"] is False
    assert body["authority"]["policy_transparent"] is True
