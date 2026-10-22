"""Production lifecycle ingestion: worker/DB, restart, forged events, three-agent E2E."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import passing_required_runs
from app.agent_coordination.lifecycle import ingest_lifecycle_event, reconcile_owner
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

HEAD = "2c9dd7e06dd3072f8f1f31531437f285ae978e5f"
PARENT = "1105e331a444c47d80d10265c4947eb1b48aeb72"


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


def _goal_task(db, owner_id, title="Lifecycle"):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title=title,
        original_instruction="Reconcile agent lifecycle without founder transcription.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db,
        goal=goal,
        rationale="lifecycle",
        tasks=[PlannedTaskSpec(description="Build", task_type="repo_edit", target_files=["docs/DURABLE_AGENT_COORDINATION.md"])],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    return goal, task


def _agent(db, key, *, write=True):
    return register_agent(db, agent_key=key, display_name=key, adapter_kind="cli", supports_write=write)


def _assign(db, owner_id, goal, task, agent, *, role="builder", key="work", paths=None):
    return create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role=role,
        read_write_mode="read_write" if role == "builder" else "read_only",
        repository_identity="lifeai",
        allowed_paths=paths or (["docs/**"] if role == "builder" else ["docs/**"]),
        requested_by="founder",
        provenance={"key": key},
    )


def _run_to(db, assignment, *statuses):
    for status in statuses:
        transition_status(db, assignment=assignment, new_status=status)
    db.commit()


def _worker_tick(db):
    """Production worker tick used by Worker.run_once(), driven on the test session."""
    Worker()._reconcile_durable_coordination(db)
    db.commit()


def test_spoofed_identity_and_forged_completion_rejected(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id)
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    assignment = _assign(db_session, owner_id, goal, task, cursor, key="spoof")
    db_session.commit()
    spoof = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="cursor",
        payload={
            "kind": "completion_claim",
            "event_id": "spoof-1",
            "assignment_id": str(assignment.id),
            "agent_identity": "claude",
        },
    )
    assert spoof["outcome"] == "forged_rejected"
    forged = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="cursor",
        payload={
            "kind": "completion_claim",
            "event_id": "forged-complete",
            "assignment_id": str(assignment.id),
            "agent_identity": "cursor",
        },
    )
    assert forged["outcome"] == "forged_rejected"
    assert "execution" in forged["reason"]


def test_duplicate_and_out_of_order_events(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, title="Dup")
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    assignment = _assign(db_session, owner_id, goal, task, cursor, key="dup")
    db_session.commit()
    early = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "execution_progress",
            "event_id": "progress-before-assign",
            "assignment_id": str(uuid.uuid4()),
            "agent_identity": "cursor",
            "evidence": {"source": "late"},
        },
    )
    assert early["outcome"] == "stale_rejected"
    first = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "assignment_observed",
            "event_id": "assign-1",
            "assignment_id": str(assignment.id),
            "agent_identity": "cursor",
        },
    )
    second = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "assignment_observed",
            "event_id": "assign-1",
            "assignment_id": str(assignment.id),
            "agent_identity": "cursor",
        },
    )
    assert first["outcome"] == "applied"
    assert second["outcome"] == "replay_ignored"


def test_github_green_is_not_independent_certification(db_session, owner_id):
    rejected = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "independent_review_receipt",
            "event_id": "ci-as-cert",
            "from_github_ci": True,
            "evidence": {"sha": HEAD},
        },
    )
    assert rejected["outcome"] == "stale_rejected"
    assert rejected["independently_verified"] is False
    delivery = ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={"kind": "delivery_claim", "event_id": "deliver-claude", "agent_identity": "claude"},
    )
    assert delivery["outcome"] == "blocked"
    assert delivery["delivery_claimed"] is False
    assert delivery["missing_capability"] == "agent_messaging_adapter"


@pytest.mark.asyncio
async def test_worker_three_agent_restart_and_follow_up(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, title="Three agents")
    plan = create_plan(
        db_session,
        goal=goal,
        rationale="codex sibling",
        tasks=[PlannedTaskSpec(description="Codex build", task_type="repo_edit", target_files=["backend/app/agent_coordination/lifecycle.py"])],
        created_by="founder",
    )
    db_session.commit()
    codex_task = db_session.query(MainAITask).filter_by(plan_id=plan.id).one()
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    claude = _agent(db_session, f"claude-{uuid.uuid4().hex[:8]}", write=False)
    codex = _agent(db_session, f"codex-{uuid.uuid4().hex[:8]}")
    cursor_a = _assign(db_session, owner_id, goal, task, cursor, key="cursor-build", paths=["docs/**"])
    claude_a = _assign(db_session, owner_id, goal, task, claude, role="reviewer", key="claude-exam", paths=["docs/**"])
    codex_a = _assign(db_session, owner_id, goal, codex_task, codex, key="codex-build", paths=["backend/**"])
    _run_to(db_session, cursor_a, "ready", "running")
    _run_to(db_session, claude_a, "ready", "running")
    _run_to(db_session, codex_a, "ready", "running")
    db_session.commit()

    _worker_tick(db_session)
    await Worker().run_once()
    first = load_owner_ledger(db_session, owner_id=owner_id)
    assert first.get(str(cursor_a.id))["status"] == "executing"
    assert first.get(str(claude_a.id))["status"] == "executing"
    assert first.get(str(codex_a.id))["status"] == "executing"

    _run_to(db_session, cursor_a, "completed")
    checks, workflows = passing_required_runs(HEAD)
    ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "github_observation",
            "event_id": "pr-published",
            "observation": {
                "number": 300,
                "title": "intake",
                "draft": False,
                "merged": False,
                "state": "open",
                "html_url": "https://github.com/d1n095/LifeAI/pull/300",
                "head_sha": HEAD,
                "base_sha": PARENT,
                "head_branch": "cursor/lifecycle-autonomous-coordination-91c1",
                "base_branch": "cursor/github-readonly-intake-91c1",
                "ancestry": [HEAD, PARENT],
                "reviews": [],
                "check_runs": checks,
                "workflow_runs": workflows,
            },
        },
    )
    db_session.commit()
    _worker_tick(db_session)

    restarted = Worker()
    restarted._reconcile_durable_coordination(db_session)
    db_session.commit()
    await restarted.run_once()
    ledger = load_owner_ledger(db_session, owner_id=owner_id)
    cursor_row = ledger.get(str(cursor_a.id))
    claude_row = ledger.get(str(claude_a.id))
    codex_row = ledger.get(str(codex_a.id))
    assert cursor_row["status"] == "completed"
    assert claude_row["status"] == "executing"
    assert codex_row["status"] == "executing"
    view = ledger.founder_view()
    assert view["first_cycle_go"] is False
    assert view["lifecycle"]["delivery_claimed"] is False
    assert view["lifecycle"]["github_green_is_independent_certification"] is False
    assert any(row.get("agent_identity") == "cursor" for row in view["lifecycle"]["builder_eligible"])
    examiners = [row for row in view["obligations"] if row["agent_identity"] == "examiner"]
    assert len(examiners) == 1
    assert examiners[0]["status"] != "independently_verified"
    snap = build_founder_ops_snapshot(db_session, owner_id=owner_id, health={"status": "ok"})
    assert snap["first_cycle_go"] is False
    assert snap["durable_coordination"]["lifecycle"]["delivery_claimed"] is False
    assert snap["durable_coordination"]["independently_certified"] == [] or all(
        row.get("agent_identity") != "cursor" for row in snap["durable_coordination"]["independently_certified"]
    )


def test_http_event_boundary_requires_founder(client):
    res = client.post("/api/mainai/founder-ops/coordination/events", json={"kind": "delivery_claim", "event_id": "x"})
    assert res.status_code in {401, 403}
