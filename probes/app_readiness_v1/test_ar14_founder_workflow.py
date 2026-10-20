"""AR-14..AR-16 — Founder-workflow truthfulness (introduced by #293: status `assigned`/`acknowledged`,
communication policy applied to every chat reply, dependency cycles, approvals).

Feature-specific: on a candidate without these modules the tests are skipped with a reason (they are not
capability gates; AR-1..AR-13 remain the gates).

  AR-14a A Founder QUESTION about GO is not answered with an invented "Nej ... evidensen motsäger påståendet".
  AR-14b A Founder claim about Recall is contradicted with a Recall statement, not the GitHub-write text.
  AR-14c A reply stating a KNOWN cost is not rewritten to say the cost is unknown.
  AR-14d The chat route does not feed hard-coded evidence (independently_verified/recall=False) into the policy.
  AR-15  A task re-queued after a failed attempt (status ready, no live job, old `dispatched` event) is not shown
         as `acknowledged` ("a worker accepted it").
  AR-16  A dependency cycle refuses new assignment for every member and is reported as a blocker / next action.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

comm = pytest.importorskip("app.mainai_founder_ops.communication", reason="candidate has no communication policy (pre-#293)")


def test_AR14a_question_about_go_is_not_contradicted():
    out = comm.apply_communication_policy(
        "First-cycle GO blockeras av transport och grants.",
        founder_message="Vad blockerar first-cycle GO?",
        evidence={"first_cycle_go": False},
    )
    assert not out.startswith("Nej."), f"AR-14a a question was answered as if the Founder had claimed GO: {out!r}"


def test_AR14b_recall_claim_gets_a_recall_disagreement():
    out = comm.apply_communication_policy("Okej.", founder_message="Recall är på nu.", evidence={"recall_http_mounted": False})
    assert "Recall" in out and "GitHub" not in out, f"AR-14b Recall claim answered with an unrelated notice: {out!r}"


def test_AR14c_known_cost_not_rewritten_as_unknown():
    out = comm.apply_communication_policy("Kostnaden hittills är 0.42 USD.", founder_message="Vad är kostnaden hittills?",
                                          evidence={"first_cycle_go": False})
    assert "okänd" not in out, f"AR-14c a known cost was annotated as unknown: {out!r}"


def test_AR14d_chat_route_does_not_hardcode_policy_evidence():
    from app.routers import chat

    src = inspect.getsource(chat)
    hard = [k for k in ('"independently_verified": False', '"recall_http_mounted": False') if k in src]
    assert not hard, f"AR-14d chat feeds hard-coded evidence into the disagreement policy: {hard}"


def test_AR15_requeued_task_is_not_acknowledged():
    from app.mainai_founder_ops.status import classify_mainai_task
    from app.models.mainai_execution import MainAITaskEventType, MainAITaskStatus
    from app.models.mainai_job import MainAIJobStatus

    task = SimpleNamespace(id=uuid.uuid4(), goal_id=uuid.uuid4(), status=MainAITaskStatus.ready, blocker_reason=None,
                           mainai_job_id=uuid.uuid4(), description="re-queued after a failed attempt", approval_required=False)
    dead_job = SimpleNamespace(status=MainAIJobStatus.failed, lease_expires_at=None, locked_by=None)
    old_dispatch = SimpleNamespace(event_type=MainAITaskEventType.dispatched)
    item = classify_mainai_task(task, dead_job, events=[old_dispatch])
    assert item["founder_status"] != "acknowledged", (
        f"AR-15 a ready task with no live job is shown as acknowledged (a worker accepted it): {item['founder_status']}")


def test_AR16_cycle_refuses_assignment_and_is_reported(superuser_db):
    from app.mainai_founder_ops.assignment_guard import DuplicateWorkError, assert_no_duplicate_task_assignment
    from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask, MainAITaskDependency
    from app.models.user import User

    owner = User(email=f"cyc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    goal = create_goal(superuser_db, owner_id=owner.id, title="g", original_instruction="g", created_by="test")
    create_plan(superuser_db, goal=goal, rationale="r", created_by="test",
                tasks=[PlannedTaskSpec(description=f"t{i}", task_type="read_only_audit") for i in range(3)])
    a, b, c = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.description).all()
    cols = {col.name for col in MainAITaskDependency.__table__.columns}
    for x, y in ((a, b), (b, c), (c, a)):
        kw = dict(task_id=x.id, depends_on_task_id=y.id)
        if "owner_id" in cols:
            kw["owner_id"] = owner.id
        superuser_db.add(MainAITaskDependency(**kw))
    superuser_db.flush()
    refused = []
    for t in (a, b, c):
        try:
            assert_no_duplicate_task_assignment(superuser_db, owner_id=owner.id, task_id=t.id)
            refused.append(False)
        except DuplicateWorkError:
            refused.append(True)
    snap = build_founder_ops_snapshot(superuser_db, owner_id=owner.id, health={"status": "ok", "worker": "unknown"})
    kinds = {x.get("kind") for x in snap.get("next_actions", [])} | {x.get("kind") for x in snap.get("blockers", [])}
    superuser_db.rollback()
    print(f"AR16|refused={refused} kinds={sorted(k for k in kinds if k)}")
    assert all(refused), f"AR-16 a task inside a dependency cycle was assignable: {refused}"
    assert kinds & {"break_circular_dependency", "circular_dependency", "dependency_cycle"}, f"AR-16 cycle not surfaced: {kinds}"
