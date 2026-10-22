"""MainAI Agent Coordination, Task-State Integrity & Operational Memory — independent acceptance (CO-*).
Against the real task/event/job/assignment/verification mechanisms in the repo. Providers/egress blocked (no paid calls).

Vocabulary: PASS = behaviour enforced by real code; FAIL = real code gets it wrong; MISSING/BLOCKED (pytest.fail) =
no mechanism exists — never a PASS. Each invariant cites the production mechanism it exercises.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401


def _goal_tasks(superuser_db, owner, n=2):
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask

    goal = create_goal(superuser_db, owner_id=owner, title="co", original_instruction="co", created_by="founder")
    create_plan(superuser_db, goal=goal, rationale="r", created_by="founder",
                tasks=[PlannedTaskSpec(description=f"t{i}", task_type="read_only_audit") for i in range(n)])
    tasks = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.description).all()
    superuser_db.commit()
    return goal, tasks


# ---- Inv 3: assignment/acknowledged/running/blocked/completed/failed distinct, evidence-grounded
def test_CO3_status_states_are_distinct_and_evidence_grounded():
    from app.mainai_founder_ops.status import FounderWorkStatus, classify_mainai_task
    from app.models.mainai_execution import MainAITaskStatus
    from app.models.mainai_job import MainAIJobStatus
    from types import SimpleNamespace

    def task(status):
        return SimpleNamespace(id=uuid.uuid4(), goal_id=uuid.uuid4(), status=status, blocker_reason=None,
                               mainai_job_id=None, description="d", approval_required=False)

    running_job = SimpleNamespace(status=MainAIJobStatus.running, lease_expires_at=datetime.utcnow() + timedelta(minutes=5), locked_by="w")
    dead = classify_mainai_task(task(MainAITaskStatus.running), SimpleNamespace(status=MainAIJobStatus.failed, lease_expires_at=None, locked_by=None))
    seen = {
        "ready->queued": classify_mainai_task(task(MainAITaskStatus.ready))["founder_status"],
        "pending->waiting": classify_mainai_task(task(MainAITaskStatus.pending))["founder_status"],
        "running(live)->running": classify_mainai_task(SimpleNamespace(id=uuid.uuid4(), goal_id=uuid.uuid4(), status=MainAITaskStatus.running, blocker_reason=None, mainai_job_id=uuid.uuid4(), description="d", approval_required=False), running_job)["founder_status"],
        "running(dead)->failed": dead["founder_status"],
        "completed->completed": classify_mainai_task(task(MainAITaskStatus.completed), events=[])["founder_status"],
        "failed->failed": classify_mainai_task(task(MainAITaskStatus.failed))["founder_status"],
    }
    print(f"CO3|{seen}")
    assert seen["ready->queued"] == "queued"
    assert seen["pending->waiting"] == "waiting_on_dependency"
    assert seen["running(live)->running"] == "running"
    assert seen["running(dead)->failed"] == "failed", "CO-3 a crashed task is still shown running"
    assert seen["completed->completed"] == "completed"
    assert seen["failed->failed"] == "failed"


# ---- Inv 1: current state must not be overridden by a stale historical completion
def test_CO1_stale_completion_event_does_not_mark_active_task_done(superuser_db, owner_id):
    from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
    from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType, MainAITaskStatus

    goal, tasks = _goal_tasks(superuser_db, owner_id)
    t = tasks[0]
    # an OLD completed-ish event, then the task is re-activated (ready) now
    superuser_db.add(MainAITaskEvent(task_id=t.id, owner_id=owner_id, event_type=MainAITaskEventType.dispatched,
                                     detail={"stale": True}, created_at=datetime.utcnow() - timedelta(days=2)))
    t.status = MainAITaskStatus.ready
    superuser_db.flush()
    superuser_db.commit()
    snap = build_founder_ops_snapshot(superuser_db, owner_id=owner_id, health={"status": "ok", "worker": "unknown"})
    item = next(i for i in snap["work_items"] if i.get("task_id") == str(t.id))
    print(f"CO1|founder_status={item['founder_status']} (task is ready now, had a stale dispatch 2d ago)")
    assert item["founder_status"] in ("queued", "acknowledged"), (
        f"CO-1 a currently-ready task is shown as {item['founder_status']} from a stale historical event")
    assert item["founder_status"] != "completed", "CO-1 stale history overrides current active state"


# ---- Inv 2: a delayed ack / restarted conversation / stale completion must not re-issue a paid dispatch
def test_CO2_completed_task_is_not_redispatchable(superuser_db, owner_id):
    from app.agent_orchestration import create_agent_task
    from app.mainai_founder_ops.assignment_guard import DuplicateWorkError, assert_agent_dispatch_allowed
    from app.models.agent_task import AgentTaskStatus

    task = create_agent_task(superuser_db, title="t", description="d", acceptance_criteria="a", created_by="test")
    for terminal in (AgentTaskStatus.reviewed_approved, AgentTaskStatus.pr_opened, AgentTaskStatus.result_recorded):
        task.status = terminal
        superuser_db.flush()
        with pytest.raises(DuplicateWorkError):
            assert_agent_dispatch_allowed(task)
    superuser_db.rollback()
    print("CO2|already-progressed agent task refuses re-dispatch for every non-redispatchable status")


def test_CO2b_duplicate_delegation_and_dispatch_guards_exist(superuser_db, owner_id):
    from app.mainai_founder_ops import assignment_guard as g

    for fn in ("assert_no_duplicate_task_assignment", "assert_agent_dispatch_allowed", "assert_no_circular_dependency"):
        assert hasattr(g, fn), f"CO-2b missing dedup guard {fn}"
    print("CO2b|dedup guards present: assignment, dispatch, cycle")


# ---- Inv 7: a stalled/crashed execution is detectable without creating a new assignment
def test_CO7_stalled_and_dead_execution_are_detected(owner_id):
    from app.mainai_execution.liveness import TaskLiveness, task_liveness
    from app.models.mainai_execution import MainAITaskStatus
    from app.models.mainai_job import MainAIJobStatus
    from types import SimpleNamespace

    t = SimpleNamespace(status=MainAITaskStatus.running)
    stalled = task_liveness(t, SimpleNamespace(status=MainAIJobStatus.running, lease_expires_at=datetime.utcnow() - timedelta(minutes=1)))
    dead = task_liveness(t, None)
    done = task_liveness(SimpleNamespace(status=MainAITaskStatus.retryable_failed), None)
    print(f"CO7|stalled={stalled} dead={dead} retryable={done}")
    assert stalled == TaskLiveness.stalled and dead == TaskLiveness.dead
    assert done == TaskLiveness.done  # retryable_failed is terminal-ish for liveness, not 'running'


# ---- Inv 6: planning / status text is not presentable as completed work
def test_CO6_runtime_contract_forbids_fabricated_background_progress():
    from app.mainai_runtime_contract import build_answer_response, sanitize_unverified_execution_claims

    resp = build_answer_response("I will keep working on this in the background and report when the job finishes.")
    print(f"CO6|mode={resp.mode} job_id={resp.job_id}")
    assert resp.job_id is None, "CO-6 a chat answer carries a job_id (claims a durable job)"
    assert "answer" in str(resp.mode)
    cleaned = sanitize_unverified_execution_claims("Jobbet har startat och körs i bakgrunden.")
    assert isinstance(cleaned, str)


def test_CO6b_independently_verified_requires_a_non_builder_receipt(superuser_db, owner_id):
    """Green CI / self-review on a SHA must not read as independently verified (reuses #294/#295 receipt binding)."""
    from app.mainai_founder_ops import status as st

    if not hasattr(st, "is_independent_exact_sha_receipt"):
        pytest.skip("pre-#294 tree without receipt-based verification")
    assert st.is_independent_exact_sha_receipt({"candidate_sha": "a" * 40, "builder_identity": "x", "examiner_identity": "x", "review_result": "PASS"}) is False
    assert st.is_independent_exact_sha_receipt({"candidate_sha": "a" * 40, "builder_identity": "cursor", "examiner_identity": "claude", "review_result": "PASS"}) is True
    print("CO6b|builder==examiner receipt rejected; builder!=examiner accepted")


# ---- Inv 10: coordination never grants authority or overrides STOP
def test_CO10_coordination_cannot_clear_stop_or_grant_authority(db_session, owner_id):
    from app.workforce.kill_switch import KillSwitchError, activate_kill_switch, clear_owner_stop, get_kill_switch

    db_session.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop")
    db_session.commit()
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})
    with pytest.raises(KillSwitchError):
        clear_owner_stop(db_session, owner_id=owner_id, founder_ack="founder_ack:written by coordination logic, not the Founder")
    db_session.rollback()
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})
    assert get_kill_switch(db_session, owner_id).active, "CO-10 STOP cleared by a fabricated ack"
    print("CO10|fabricated founder_ack cannot clear STOP")


# ---- Inv 4: cross-agent ownership — builder != examiner receipts exist; a single-obligation ledger does NOT
def test_CO4_cross_agent_receipts_track_distinct_builder_and_examiner(superuser_db, owner_id):
    reg = pytest.importorskip("app.mainai_verification_registry.service")
    with pytest.raises(Exception):
        reg.record_verification_attestation(superuser_db, component_id="c", candidate_sha="a" * 40, builder_identity="cursor",
                                            examiner_identity="cursor", review_result="PASS", owner_id=owner_id, evidence_summary="x")
    print("CO4|verification registry rejects builder==examiner (self-certification)")


def test_CO4b_cross_agent_current_obligation_ledger_is_present():
    """MISSING check: a mechanism that tracks, per independent agent (Cursor/Codex/examiner), its CURRENT obligation
    and dependency timeline — distinct from per-SHA receipts and per-owner task rows."""
    candidates = ["app.mainai_coordination", "app.agent_coordination.obligations", "app.mainai_execution.obligations"]
    import importlib

    found = []
    for m in candidates:
        try:
            importlib.import_module(m)
            found.append(m)
        except ImportError:
            pass
    print(f"CO4b|obligation_ledger_modules={found}")
    if not found:
        pytest.fail("MISSING: no cross-agent current-obligation / ownership-timeline ledger. Coordination relies on "
                    "per-owner task rows + per-SHA verification receipts; separate agent timelines (inv 4) and "
                    "current-vs-historical obligation (inv 1) are not modeled as first-class state.")


# ---- Inv 5: a missing artifact is a truthful BLOCKED; authorized auto-transfer is MISSING (the courier gap)
def test_CO5_authorized_artifact_transfer_exists():
    """MISSING check: when a required artifact (e.g. a candidate bundle) is absent, is there an AUTHORIZED automatic
    transfer/import path, so MainAI does not repeatedly ask the Founder to act as a file courier (inv 5)?"""
    import importlib

    found = []
    for m in ("app.mainai_coordination.transfer", "app.artifact_transfer", "app.mainai_execution.artifact_intake"):
        try:
            importlib.import_module(m)
            found.append(m)
        except ImportError:
            pass
    print(f"CO5|artifact_transfer_modules={found}")
    if not found:
        pytest.fail("MISSING: no authorized automatic artifact-transfer mechanism. A missing artifact today requires "
                    "a manual Founder hand-off (observed directly: the Codex bundle). Architectural gap, not a "
                    "first-cycle blocker; see remediation contract.")
