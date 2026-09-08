from datetime import datetime, timedelta, timezone

import pytest

from app.mainai_execution.substrate import ExecutionSubstrate
from app.mainai_execution.supervision import (
    AgentObservation,
    AgentState,
    BlockerClass,
    ContinuousSupervisor,
    classify_blocker,
    validate_cost,
)


def _obs(state, *, job_id=None, attempt_id=None, nonce="n1"):
    return AgentObservation("agent-a", "alice", state, nonce, 1, job_id, attempt_id, "fake", datetime.now(timezone.utc).isoformat(), "p1")


def test_idle_current_job_gets_one_idempotent_continuation(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    supervisor.observe(_obs(AgentState.IDLE, job_id="job-1", attempt_id="attempt-1"))
    first = supervisor.supervise(owner_id="alice", agent_id="agent-a")
    second = supervisor.supervise(owner_id="alice", agent_id="agent-a")
    assert len(first) == len(second) == 1
    assert first[0].message_id == second[0].message_id


def test_healthy_running_is_not_interrupted(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    supervisor.observe(_obs(AgentState.RUNNING, job_id="job-1", attempt_id="attempt-1"))
    assert supervisor.supervise(owner_id="alice", agent_id="agent-a") == []


def test_idle_without_current_job_gets_one_ready_job(tmp_path):
    substrate = ExecutionSubstrate(tmp_path / "state.sqlite")
    job = substrate.submit_job(owner_id="alice", program="p", max_active=10)
    supervisor = ContinuousSupervisor(substrate)
    supervisor.observe(_obs(AgentState.IDLE))
    message = supervisor.supervise(owner_id="alice", agent_id="agent-a")[0]
    assert message.kind == "START_NEXT_READY_JOB" and message.job_id == job.job_id


def test_result_submitted_requests_verification_without_continuation(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    supervisor.observe(_obs(AgentState.RESULT_SUBMITTED, job_id="job-1", attempt_id="attempt-1"))
    assert supervisor.supervise(owner_id="alice", agent_id="agent-a") == []
    with supervisor._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM supervision_events WHERE event_type='JOB_VERIFICATION_REQUIRED'").fetchone()[0] == 1


def test_owner_scope_and_late_nonce_do_not_authorize_other_owner(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    supervisor.observe(_obs(AgentState.IDLE, job_id="job-1", attempt_id="attempt-1"))
    with pytest.raises(RuntimeError):
        supervisor.supervise(owner_id="bob", agent_id="agent-a")
    supervisor.observe(_obs(AgentState.IDLE, job_id="job-1", attempt_id="attempt-2", nonce="n2"))
    assert supervisor.supervise(owner_id="alice", agent_id="agent-a")[0].attempt_id == "attempt-2"


def test_message_delivery_is_bounded_and_dead_letters(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    supervisor.continuation(owner_id="alice", agent_id="agent-a", job_id="job-1", attempt_id="attempt-1", reason="continue", sequence=1)
    assert supervisor.deliver(lambda _: (_ for _ in ()).throw(ValueError("poison")), owner_id="alice", max_attempts=1) == (0, 1, 0)
    assert supervisor.deliver(lambda _: None, owner_id="alice") == (0, 0, 0)


def test_budget_reservation_binds_releases_and_expires(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    reservation = supervisor.reserve_budget(owner_id="alice", job_id="job-1", amount=2, ttl_seconds=1)
    supervisor.bind_budget(reservation, "attempt-1")
    assert supervisor.release_budget(reservation)
    expired = supervisor.reserve_budget(owner_id="alice", job_id="job-2", amount=1, ttl_seconds=1)
    assert supervisor.reconcile_budget(now=datetime.now(timezone.utc) + timedelta(seconds=2)) == 1
    assert expired != reservation


def test_blocker_classifier_does_not_accept_missing_code_as_external_blocker():
    assert classify_blocker("missing test harness").classification is BlockerClass.INCOMPLETE_IMPLEMENTATION
    assert classify_blocker("founder approval is required").founder_required
    assert classify_blocker("provider unavailable", external_unavailable=True).retryable


def test_cost_validation_fails_closed():
    assert validate_cost(reserved=1, reported=0.5, prompt_tokens=10, completion_tokens=10, unit_price=0.01)
    assert not validate_cost(reserved=1, reported=2, prompt_tokens=0, completion_tokens=0)
    assert not validate_cost(reserved=1, reported=0.1, prompt_tokens=1000, completion_tokens=1000, unit_price=0.01)


def test_deterministic_thousand_job_supervision_soak(tmp_path):
    supervisor = ContinuousSupervisor(ExecutionSubstrate(tmp_path / "state.sqlite"))
    for index in range(1000):
        owner = f"owner-{index % 3}"
        agent = f"agent-{index % 5}"
        supervisor.observe(AgentObservation(agent, owner, AgentState.IDLE, f"nonce-{index}", index, f"job-{index}", f"attempt-{index}", "fake", datetime.now(timezone.utc).isoformat(), f"progress-{index}"))
        supervisor.continuation(owner_id=owner, agent_id=agent, job_id=f"job-{index}", attempt_id=f"attempt-{index}", reason="resume authorized unfinished work", sequence=1)
        supervisor.observe(AgentObservation(agent, owner, AgentState.PROGRESSING, f"nonce-{index}", index, f"job-{index}", f"attempt-{index}", "fake", datetime.now(timezone.utc).isoformat(), f"progress-{index + 1}"))
    delivered = supervisor.deliver(lambda _: None, owner_id="owner-0", limit=1000)
    assert delivered[0] == 334
    with supervisor._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM supervision_messages").fetchone()[0] == 1000
        assert db.execute("SELECT COUNT(*) FROM supervision_events").fetchone()[0] == 3000
