"""Application-readiness Founder ops: truthful status, STOP, no fabricated progress.

Covers the launch-blocking behaviors the website/MainAI interface must not get wrong:
  A. assigned != running (never infer activity from an assignment).
  B. independently_verified requires durable independent evidence; self-review is not it.
  C. stale/failed liveness is failed, not still running.
  D. duplicate live assignment and duplicate paid dispatch are refused.
  E. snapshot keeps task context (id, description, blocker) and never sets first_cycle_go.
  F. HTTP is founder-only; STOP observability fail-closed; conversation reload keeps failures.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.agent_orchestration import create_agent_task, dispatch_task, get_task
from app.bootstrap import bootstrap_founder_user
from app.config import get_settings
from app.founder import FOUNDER_USER_ID
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.assignment_guard import DuplicateWorkError, assert_agent_dispatch_allowed
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import (
    classify_agent_task,
    classify_mainai_job,
    classify_mainai_task,
    classify_workforce_assignment,
)
from app.models.agent_task import AgentTaskStatus
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.workforce import resolve_delegation, submit_delegation_request
from app.workforce.broker import DelegationBrokerError
from app.workforce.registry import register_workforce_agent

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _task(*, status, blocker_reason=None, job_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        goal_id=uuid.uuid4(),
        status=status,
        blocker_reason=blocker_reason,
        mainai_job_id=job_id,
        description="keep this context",
    )


def _assignment(**overrides):
    values = dict(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        verifier_profile_id=uuid.uuid4(),
        status="assigned",
        verification_status="UNVERIFIED",
        revoked_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_assignment_is_assigned_not_running():
    item = classify_workforce_assignment(_assignment(status="assigned"))
    assert item["founder_status"] == "assigned"
    assert item["independently_verified"] is False
    assert item["founder_status"] != "running"


def test_acknowledged_job_is_not_running():
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status=MainAIJobStatus.queued,
        locked_by="worker-1",
        job_type="task_execution",
        public_message=None,
        error_category=None,
    )
    item = classify_mainai_job(job)
    assert item["founder_status"] == "acknowledged"
    assert item["locked_by"] == "worker-1"


def test_queued_job_without_lock_is_queued_not_running():
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status=MainAIJobStatus.queued,
        locked_by=None,
        job_type="task_execution",
        public_message=None,
        error_category=None,
    )
    assert classify_mainai_job(job)["founder_status"] == "queued"


def test_agent_created_is_assigned_not_running():
    task = SimpleNamespace(id=uuid.uuid4(), title="x", status=AgentTaskStatus.created)
    item = classify_agent_task(task)
    assert item["founder_status"] == "assigned"
    assert item["independently_verified"] is False


def test_running_assignment_is_running_only_when_status_says_so():
    item = classify_workforce_assignment(_assignment(status="running"))
    assert item["founder_status"] == "running"


def test_awaiting_verification_is_waiting_not_independently_verified():
    item = classify_workforce_assignment(_assignment(status="awaiting_verification"))
    assert item["founder_status"] == "waiting_on_dependency"
    assert item["independently_verified"] is False


def test_self_verification_is_not_independently_verified():
    profile = uuid.uuid4()
    item = classify_workforce_assignment(
        _assignment(
            status="completed",
            verification_status="VERIFIED",
            profile_id=profile,
            verifier_profile_id=profile,
        )
    )
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False
    assert item["unjustified_self_verification"] is True


def test_independent_verifier_can_mark_verified():
    item = classify_workforce_assignment(
        _assignment(status="completed", verification_status="VERIFIED")
    )
    assert item["founder_status"] == "independently_verified"


def test_stalled_running_task_is_failed_not_running():
    job = SimpleNamespace(
        status=MainAIJobStatus.running,
        lease_expires_at=datetime.utcnow() - timedelta(minutes=5),
    )
    item = classify_mainai_task(_task(status=MainAITaskStatus.running, job_id=uuid.uuid4()), job)
    assert item["founder_status"] == "failed"
    assert item["failure_class"] == "stalled"
    assert item["description"] == "keep this context"


def test_pending_task_is_waiting_on_dependency_and_keeps_blocker():
    item = classify_mainai_task(_task(status=MainAITaskStatus.pending, blocker_reason="needs plan"))
    assert item["founder_status"] == "waiting_on_dependency"
    assert item["blocker_reason"] == "needs plan"
    assert item["task_id"]


def test_completed_without_verification_event_is_completed_not_verified():
    item = classify_mainai_task(_task(status=MainAITaskStatus.completed), events=[])
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False


def test_completed_with_verification_passed_is_not_independently_verified():
    event = SimpleNamespace(event_type=MainAITaskEventType.verification_passed)
    item = classify_mainai_task(_task(status=MainAITaskStatus.completed), events=[event])
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False
    assert item["builder_self_verification"] is True


def test_dispatched_event_without_running_lease_is_acknowledged_not_running():
    event = SimpleNamespace(event_type=MainAITaskEventType.dispatched)
    current_job = SimpleNamespace(
        id=uuid.uuid4(),
        status=MainAIJobStatus.queued,
        locked_by=None,
        job_type="task_execution",
        public_message=None,
        error_category=None,
    )
    item = classify_mainai_task(
        _task(status=MainAITaskStatus.ready, job_id=current_job.id),
        current_job,
        events=[event],
    )
    assert item["founder_status"] == "acknowledged"
    assert item["founder_status"] != "running"
    assert item["current_acknowledgment"] is True
    assert item["description"] == "keep this context"


def test_historical_dispatch_without_live_job_is_not_current_acknowledgment():
    event = SimpleNamespace(event_type=MainAITaskEventType.dispatched)
    item = classify_mainai_task(_task(status=MainAITaskStatus.ready), events=[event])
    assert item["founder_status"] == "queued"
    assert item["dispatched"] is True
    assert item["current_acknowledgment"] is False
    assert item["founder_status"] != "acknowledged"


def test_agent_self_review_is_completed_not_independently_verified():
    task = SimpleNamespace(id=uuid.uuid4(), title="x", status=AgentTaskStatus.reviewed_approved)
    item = classify_agent_task(task)
    assert item["founder_status"] == "completed"
    assert item["independently_verified"] is False


@pytest.mark.asyncio
async def test_duplicate_dispatch_is_refused(db_session, monkeypatch):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content="diff", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    task = create_agent_task(
        db_session,
        title="Ett uppdrag",
        description="Gör en sak.",
        acceptance_criteria="Tester gröna.",
        created_by="test",
    )
    await dispatch_task(db_session, task.id, dispatched_by="test")
    refreshed = get_task(db_session, task.id)
    assert refreshed.status == AgentTaskStatus.result_recorded
    with pytest.raises(DuplicateWorkError, match="duplicate_dispatch"):
        assert_agent_dispatch_allowed(refreshed)
    with pytest.raises(DuplicateWorkError):
        await dispatch_task(db_session, task.id, dispatched_by="test")


def test_duplicate_live_assignment_for_same_task_is_refused(superuser_db):
    owner = User(email=f"ops-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    builder = register_workforce_agent(
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
    verifier = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="local-verifier",
        name="Local Verifier",
        role="verifier",
        agent_type="VERIFIER",
        provider_type="local",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    goal = create_goal(
        superuser_db,
        owner_id=owner.id,
        title="ops goal",
        original_instruction="do it",
        created_by="test",
    )
    create_plan(
        superuser_db,
        goal=goal,
        rationale="one",
        tasks=[PlannedTaskSpec(description="the work", task_type="read_only_audit")],
        created_by="test",
    )
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="classify",
        required_capability="low_risk_classification",
        task_id=task.id,
        verification_requirement="independent_verifier",
    )
    resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=verifier.id)
    superuser_db.commit()
    with pytest.raises(DelegationBrokerError, match="duplicate_"):
        submit_delegation_request(
            superuser_db,
            owner_id=owner.id,
            goal_text="classify again",
            required_capability="low_risk_classification",
            task_id=task.id,
            verification_requirement="independent_verifier",
        )
    assert builder.id


def test_snapshot_never_claims_go_and_keeps_task_context(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    goal = create_goal(
        db_session,
        owner_id=FOUNDER_USER_ID,
        title="context goal",
        original_instruction="keep ids",
        created_by="test",
    )
    create_plan(
        db_session,
        goal=goal,
        rationale="one",
        tasks=[PlannedTaskSpec(description="unique task context", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok", "worker": "unknown"})
    assert snap["first_cycle_go"] is False
    assert snap["fabricated_progress"] is False
    assert snap["safe_launch"]["github_write_enabled"] is False
    assert snap["safe_launch"]["recall_http_mounted"] is False
    descriptions = [item.get("description") for item in snap["work_items"]]
    assert "unique task context" in descriptions
    matching = next(item for item in snap["work_items"] if item.get("description") == "unique task context")
    assert matching["task_id"]
    assert matching["goal_id"] == str(goal.id)
    assert "agents" in snap
    assert "next_actions" in snap
    assert "independent_ready_work" in snap
    assert snap["circular_dependencies"] == []
    assert matching["task_id"] in str(snap["work_items"])


def test_app_registers_founder_ops_routes():
    """Locks the Pydantic forward-ref crash that made every backend/E2E job fail on import."""
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    methods = {
        getattr(route, "path", ""): set(getattr(route, "methods", set()) or set())
        for route in app.routes
    }
    assert "/api/mainai/founder-ops/snapshot" in paths
    assert "/api/mainai/founder-ops/stop" in paths
    assert "GET" in methods["/api/mainai/founder-ops/snapshot"]
    assert "POST" in methods["/api/mainai/founder-ops/stop"]


def test_unavailable_health_is_not_reported_reachable(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    snap = build_founder_ops_snapshot(
        db_session,
        owner_id=FOUNDER_USER_ID,
        health={"status": "unavailable", "worker": "unknown"},
    )
    assert snap["health"]["backend_reachable"] is False
    assert snap["health"]["status"] == "unavailable"
    assert snap["first_cycle_go"] is False
    assert snap["fabricated_progress"] is False


def test_snapshot_http_is_founder_only(client, make_verified_user):
    assert client.get("/api/mainai/founder-ops/snapshot").status_code in (401, 403)
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    denied = client.get("/api/mainai/founder-ops/snapshot")
    assert denied.status_code == 403
    _login(client)
    ok = client.get("/api/mainai/founder-ops/snapshot")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["first_cycle_go"] is False
    assert body["health"]["status"] in {"ok", "unavailable"}
    assert "queued" in body["work_item_counts"] or body["work_item_counts"] == {} or isinstance(body["work_items"], list)
    assert body["spend"]["spend_live"] is True
    assert "provider_invoke_disabled" not in body["spend"]
    assert isinstance(body["agents"], list)
    assert isinstance(body["next_actions"], list)
    assert isinstance(body["independent_ready_work"], list)
    assert isinstance(body["circular_dependencies"], list)
    assert body["communication_policy"] == "factual_accuracy_over_agreement"


def test_stop_http_activates_owner_stop(client):
    csrf = _login(client)
    before = client.get("/api/mainai/founder-ops/stop")
    assert before.status_code == 200, before.text
    activated = client.post(
        "/api/mainai/founder-ops/stop",
        json={"reason": "application-readiness regression"},
        headers={"X-CSRF-Token": csrf},
    )
    assert activated.status_code == 200, activated.text
    after = client.get("/api/mainai/founder-ops/stop")
    assert after.status_code == 200
    assert after.json()["blocked"] is True
    snap = client.get("/api/mainai/founder-ops/snapshot")
    assert snap.json()["stop"]["blocked"] is True


def test_kill_switch_observability_fail_closed(monkeypatch):
    from app.mainai_executive import observability as obs

    def _boom(*args, **kwargs):
        raise RuntimeError("epoch missing")

    monkeypatch.setattr("app.workforce.kill_switch.get_kill_switch", _boom)
    payload = obs._kill_switch_status(SimpleNamespace(), uuid.uuid4())
    assert payload["blocked"] is True
    assert payload["observability_failed"] is True
    assert payload.get("active") is not False


def test_conversation_reload_keeps_failed_assistant_status(client, monkeypatch):
    dim = get_settings().embedding_dim

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * dim for _ in texts]

    async def _fail(self, messages, model, **kwargs):
        from app.providers.base import ProviderError

        raise ProviderError("OpenAI API-nyckel saknas.")

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fail)
    csrf = _login(client)
    sent = client.post("/api/chat", json={"message": "spara felet"}, headers={"X-CSRF-Token": csrf})
    assert sent.status_code == 200, sent.text
    conversation_id = sent.json()["conversation_id"]
    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    assistants = [m for m in reloaded.json()["messages"] if m["role"] == "assistant"]
    assert assistants, "failed assistant turn must remain visible after reload"
    assert assistants[0]["status"] == "failed"
    assert assistants[0]["in_reply_to_id"]
