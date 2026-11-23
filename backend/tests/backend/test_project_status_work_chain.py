"""Founder Alpha R15: the live work chain is visible, honest and read-only.

Covers the integration itself, not a helper:

  W1  "waiting for approval" uses the SAME policy function the dispatch gate calls, so the
      queue the founder sees is the queue dispatch would actually enforce;
  W2  a recorded approval removes the task from that queue (no second source of truth);
  W3  a job whose lease died is REPORTED and left untouched — only the worker may reclaim it;
  W4  a queued job nobody claimed becomes visible after the threshold;
  W5  near-identical work already completed is surfaced as a possible duplicate, never blocked;
  W6  next_steps name the approval/job with their source, and stay `authorized: False`;
  W7  the founder-only HTTP surface carries it, and reading it writes nothing;
  W8  the conversation prompt carries the work chain (so "vad händer?" is answered from facts).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text as sa_text

from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.user import User
from app.project_status.service import build_project_status, format_project_status_instructions
from app.project_status.work_chain import build_work_chain
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def founder(client, db_session):
    user = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    assert user is not None, "bootstrap must create the founder"
    _set_rls_user(db_session, user.id)
    return user


def _login(client, email: str = FOUNDER_EMAIL, password: str = FOUNDER_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


_TERMINAL_GOALS = (MainAIGoalStatus.completed, MainAIGoalStatus.failed, MainAIGoalStatus.cancelled)
_TERMINAL_TASKS = (MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled)


def _goal(db, owner_id, title, status=MainAIGoalStatus.running, policy="standard_repo_work") -> MainAIGoal:
    row = MainAIGoal(
        owner_id=owner_id,
        title=title,
        original_instruction="test",
        status=status,
        approval_policy=policy,
        created_by="founder",
        completed_at=datetime.utcnow() if status in _TERMINAL_GOALS else None,
    )
    db.add(row)
    db.commit()
    return row


def _plan(db, goal: MainAIGoal) -> MainAIPlan:
    plan = MainAIPlan(
        goal_id=goal.id,
        owner_id=goal.owner_id,
        version=1,
        status=MainAIPlanStatus.active,
        rationale="testplan",
        created_by="founder",
    )
    db.add(plan)
    db.commit()
    return plan


def _task(
    db,
    goal: MainAIGoal,
    plan: MainAIPlan,
    *,
    description: str = "Granska och förbättra modul",
    task_type: str = "repo_edit",
    status: MainAITaskStatus = MainAITaskStatus.ready,
    approval_required: bool = False,
) -> MainAITask:
    task = MainAITask(
        goal_id=goal.id,
        plan_id=plan.id,
        owner_id=goal.owner_id,
        description=description,
        task_type=task_type,
        status=status,
        approval_required=approval_required,
        verification_plan=[],
        completed_at=datetime.utcnow() if status in _TERMINAL_TASKS else None,
    )
    db.add(task)
    db.commit()
    return task


def _job(
    db,
    owner_id,
    *,
    status: MainAIJobStatus = MainAIJobStatus.running,
    age_minutes: int = 0,
    lease_delta_minutes: int | None = None,
) -> MainAIJob:
    created = datetime.utcnow() - timedelta(minutes=age_minutes)
    job = MainAIJob(
        owner_id=owner_id,
        job_type="task_execution",
        status=status,
        created_by="founder",
        input_refs=[{"type": "mainai_task", "id": str(uuid.uuid4())}],
        created_at=created,
        started_at=created if status != MainAIJobStatus.queued else None,
        last_heartbeat_at=created if status == MainAIJobStatus.running else None,
        lease_expires_at=(
            datetime.utcnow() + timedelta(minutes=lease_delta_minutes) if lease_delta_minutes is not None else None
        ),
    )
    db.add(job)
    db.commit()
    return job


# --- W1/W2 -------------------------------------------------------------------------------------


def test_awaiting_approval_uses_the_dispatch_gate_policy(db_session, founder):
    goal = _goal(db_session, founder.id, "Förbättra GitHub-statusen")
    plan = _plan(db_session, goal)
    needs = _task(db_session, goal, plan, approval_required=True, description="Skriv om service.py")
    auto = _task(db_session, goal, plan, task_type="read_only_audit", description="Läs igenom koden")

    chain = build_work_chain(db_session, owner_id=founder.id)
    ids = [row["task_id"] for row in chain["tasks"]["awaiting_approval"]]

    assert str(needs.id) in ids
    assert str(auto.id) not in ids, "a read_only_audit under standard_repo_work is auto — not a pending decision"
    assert chain["goals"]["active_count"] == 1
    assert chain["goals"]["active"][0]["plan_version"] == 1
    assert chain["tasks"]["awaiting_approval"][0]["source"] == f"mainai_tasks:{needs.id}"


def test_recorded_approval_removes_the_task_from_the_queue(db_session, founder):
    goal = _goal(db_session, founder.id, "Ett mål som väntar")
    plan = _plan(db_session, goal)
    task = _task(db_session, goal, plan, approval_required=True)
    assert build_work_chain(db_session, owner_id=founder.id)["tasks"]["awaiting_approval_count"] == 1

    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=founder.id,
            event_type=MainAITaskEventType.approval_granted,
            detail={"approved_by": FOUNDER_EMAIL},
        )
    )
    db_session.commit()

    chain = build_work_chain(db_session, owner_id=founder.id)
    assert chain["tasks"]["awaiting_approval_count"] == 0, "the approval event is the same one dispatch checks"


# --- W3/W4 -------------------------------------------------------------------------------------


def test_dead_lease_is_reported_and_never_modified(db_session, founder):
    job = _job(db_session, founder.id, status=MainAIJobStatus.running, age_minutes=90, lease_delta_minutes=-60)

    chain = build_work_chain(db_session, owner_id=founder.id)
    stalled = chain["jobs"]["stalled"]

    assert chain["jobs"]["stalled_count"] == 1
    assert stalled[0]["job_id"] == str(job.id)
    assert "lease" in stalled[0]["reason"]
    assert chain["jobs"]["counts"]["running"] == 1

    db_session.expire_all()
    assert db_session.get(MainAIJob, job.id).status == MainAIJobStatus.running, "reporting must not stop the job"


def test_unclaimed_queued_job_becomes_visible_after_the_threshold(db_session, founder):
    _job(db_session, founder.id, status=MainAIJobStatus.queued, age_minutes=1)
    old = _job(db_session, founder.id, status=MainAIJobStatus.queued, age_minutes=120)

    chain = build_work_chain(db_session, owner_id=founder.id)
    reported = {row["job_id"] for row in chain["jobs"]["stalled"]}
    assert reported == {str(old.id)}


# --- W5 ----------------------------------------------------------------------------------------


def test_possible_duplicate_of_completed_work_is_surfaced_not_blocked(db_session, founder):
    _goal(db_session, founder.id, "Förbättra GitHub-statusen med CI-evidens", status=MainAIGoalStatus.completed)
    active = _goal(db_session, founder.id, "Förbättra GitHub-statusen med CI evidens")

    chain = build_work_chain(db_session, owner_id=founder.id)
    duplicates = chain["suspected_duplicates"]

    assert any(d["goal_id"] == str(active.id) for d in duplicates)
    assert duplicates[0]["matches_completed_goal"]["title"].startswith("Förbättra GitHub-statusen")
    assert chain["goals"]["active_count"] == 1, "detection never removes or blocks the goal"


# --- W6/W7/W8 ----------------------------------------------------------------------------------


def test_next_steps_carry_the_approval_with_its_source(client, db_session, founder):
    goal = _goal(db_session, founder.id, "Mål med godkännandekrav")
    plan = _plan(db_session, goal)
    task = _task(db_session, goal, plan, approval_required=True, description="Ändra backend/app/x.py")

    _login(client)
    body = client.get("/api/mainai/project-status").json()

    approvals = [s for s in body["next_steps"] if s["kind"] == "approve_task"]
    assert approvals, body["next_steps"]
    assert approvals[0]["source"] == f"mainai_tasks:{task.id}"
    assert approvals[0]["authorized"] is False
    assert any("väntar på grundarens godkännande" in w for w in body["warnings"])


def test_http_surface_exposes_the_work_chain_and_writes_nothing(client, db_session, founder):
    goal = _goal(db_session, founder.id, "Ett aktivt mål")
    plan = _plan(db_session, goal)
    _task(db_session, goal, plan, approval_required=True)
    _job(db_session, founder.id, status=MainAIJobStatus.queued, age_minutes=60)

    counts_before = {
        "goals": db_session.execute(select(func.count()).select_from(MainAIGoal)).scalar(),
        "tasks": db_session.execute(select(func.count()).select_from(MainAITask)).scalar(),
        "jobs": db_session.execute(select(func.count()).select_from(MainAIJob)).scalar(),
    }

    _login(client)
    res = client.get("/api/mainai/project-status")
    assert res.status_code == 200
    body = res.json()

    assert body["work"]["goals"]["active_count"] == 1
    assert body["work"]["jobs"]["stalled_count"] == 1
    assert body["work"]["tasks"]["awaiting_approval_count"] == 1
    assert body["work"]["authorizations"]["pending_scope_proposals_count"] == 0

    counts_after = {
        "goals": db_session.execute(select(func.count()).select_from(MainAIGoal)).scalar(),
        "tasks": db_session.execute(select(func.count()).select_from(MainAITask)).scalar(),
        "jobs": db_session.execute(select(func.count()).select_from(MainAIJob)).scalar(),
    }
    assert counts_after == counts_before, "the work chain is a read, never a queue mutation"


def test_prompt_carries_the_live_work_chain(db_session, founder):
    goal = _goal(db_session, founder.id, "Mål som syns i samtalet")
    plan = _plan(db_session, goal)
    _task(db_session, goal, plan, approval_required=True)

    prompt = format_project_status_instructions(db_session, owner_id=founder.id)
    assert "PROJEKTSTATUS" in prompt
    assert "Arbetskedja just nu:" in prompt
    assert "väntar på godkännande" in prompt
    assert "Hitta aldrig" in prompt, "the grounding guard must be part of the prompt"


def test_unauthenticated_work_chain_read_is_rejected(client):
    assert client.get("/api/mainai/project-status").status_code == 401


def test_project_status_requires_founder(client, make_verified_user):
    user, password = make_verified_user()
    _login(client, email=user.email, password=password)
    assert client.get("/api/mainai/project-status").status_code == 403
