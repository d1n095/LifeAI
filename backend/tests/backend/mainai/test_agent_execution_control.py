"""Interactive Agent Execution Control Foundation -- proves the output-streaming durability
policy, the fail-closed interactive control contract (instruction/status/cancel/resume gated
on truthfully-declared `AdapterCapabilities`), long-running process tracking
(`AgentDispatchExecution`, migration 0047), the reconnect/recovery classifier (never a false
COMPLETE), and the founder-controlled credential-reference/env-allowlist config boundary.

`_FakeStreamingAgentAdapter` is a deterministic, CLEARLY LABELLED fake declaring full control
capabilities, used to prove the streaming/instruction/cancel/resume control loop end to end.
The lost-process and timeout scenarios instead use `LocalCLIAdapter` against harmless,
already-installed system binaries (`/bin/sleep`, a nonexistent path) -- a genuinely real
subprocess mechanism, never a real coding-agent CLI -- exactly like
test_agent_real_execution_bridge.py's own precedent. No real Claude Code/Cursor Agent/Codex
invocation happens anywhere in this file."""

import uuid

import pytest

from app.agent_coordination.adapter_config import credential_reference, resolve_adapter_env
from app.agent_coordination.adapters import (
    AdapterCapabilities,
    AdapterProcessLostError,
    AgentHealth,
    AgentObservation,
    AgentResult,
    LocalCLIAdapter,
    NotConfiguredAdapter,
    ProviderNotConfiguredError,
)
from app.agent_coordination.dispatch import OUTCOME_ADAPTER_TIMEOUT, dispatch_assignment
from app.agent_coordination.execution_control import (
    DURABLE_EVENT_KINDS,
    OUTCOME_OK,
    OUTCOME_UNSUPPORTED_CAPABILITY,
    RECONCILE_ADAPTER_DISCONNECTED,
    RECONCILE_PROCESS_ALIVE,
    RECONCILE_RESULT_PENDING_INGESTION,
    RECONCILE_RESULT_UNAVAILABLE,
    RECONCILE_SESSION_LOST,
    ExecutionEvent,
    cancel_execution,
    collect_and_ingest_execution_result,
    mark_execution_running,
    reconcile_execution_state,
    record_execution_event,
    request_execution_status,
    resume_execution,
    send_execution_instruction,
    start_execution_tracking,
)
from app.agent_coordination.service import acquire_lease, create_work_assignment, register_agent
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentWorkAssignmentEvent, AgentWorkAssignmentEventType, ExecutionAdapterState, ExecutionResultIngestionStatus
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as the other agent_coordination test modules' own identical
    fixture."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Execution control test.", approval_policy="standard_repo_work"):
    goal = create_goal(db, owner_id=owner_id, title="Execution control test", original_instruction=instruction, created_by="founder", approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="execution control test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, plan, task


def _agent(db, key, **kwargs):
    defaults = dict(display_name=key, adapter_kind="cli", execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role, mode, paths, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=list(paths), requested_by="test", **kwargs
    )


def _lease(db, assignment, agent, *, branch, worktree, paths, mode="read_write"):
    return acquire_lease(db, assignment=assignment, agent_id=agent.id, branch=branch, worktree_path=worktree, allowed_paths=list(paths), mode=mode)


def _ready_dispatchable_assignment(db, owner_id, *, approval_policy="autonomous_development_work"):
    """Builds one assignment through the FULL real gate (goal/task/approval/lease) so
    `dispatch_assignment()` genuinely succeeds -- the common setup every control-loop test
    below needs before it can exercise execution-control on top of a REAL running dispatch."""

    goal, _plan, task = _goal_plan_task(db, owner_id, approval_policy=approval_policy)
    agent = _agent(db, "claude-code")
    assignment = _assign(db, owner_id=owner_id, goal=goal, task=task, agent=agent, role="builder", mode="read_write", paths=["backend/app/execution_control_test/**"])
    _lease(db, assignment, agent, branch="claude/exec-control-test", worktree="/tmp/wt-exec-control", paths=["backend/app/execution_control_test/**"])
    grant_task_approval(db, task=task, approved_by="founder")
    db.commit()
    return assignment, agent


class _FakeStreamingAgentAdapter:
    """Deterministic test double declaring FULL control-contract support -- lives HERE, in the
    test file, never in production code. CLEARLY LABELLED FAKE. Touches no subprocess, no
    network, no credential. Tracks its own internal state so tests can assert the control
    loop actually reached the adapter, not merely that execution_control's own bookkeeping
    updated."""

    def __init__(self, *, succeed: bool = True):
        self.state = "not_started"
        self.instructions: list[str] = []
        self.cancelled = False
        self.resumed = False
        self._succeed = succeed

    async def health(self):
        return AgentHealth(healthy=True, detail="fake streaming adapter, always healthy")

    def capabilities(self):
        return ("repo_edit", "run_tests")

    def control_capabilities(self):
        return AdapterCapabilities(supports_streaming=True, supports_instruction=True, supports_resume=True, supports_cancel=True, supports_structured_events=True)

    async def start_assignment(self, assignment_id):
        self.state = "running"

    async def send_instruction(self, assignment_id, instruction):
        self.instructions.append(instruction)

    async def observe(self, assignment_id):
        return AgentObservation(raw_status=self.state)

    async def cancel(self, assignment_id):
        self.cancelled = True
        self.state = "exited"

    async def resume(self, assignment_id):
        self.resumed = True
        self.state = "running"

    async def collect_result(self, assignment_id):
        self.state = "exited"
        return AgentResult(succeeded=self._succeed, summary="fake streaming result")


# ============================================================================ Requirement 1:
# output streaming model -- durability policy.

@pytest.mark.asyncio
async def test_record_execution_event_persists_durable_kinds_and_updates_heartbeat(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake", attempt_id=uuid.uuid4())

    record_execution_event(superuser_db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="progress", summary="50% done"))
    superuser_db.refresh(execution)
    assert execution.last_heartbeat_at is not None

    events = superuser_db.query(AgentWorkAssignmentEvent).filter_by(
        assignment_id=assignment.id, event_type=AgentWorkAssignmentEventType.execution_observed
    ).all()
    assert len(events) == 1
    assert events[0].detail["kind"] == "progress"
    assert events[0].detail["attempt_id"] == str(execution.attempt_id)


@pytest.mark.asyncio
async def test_record_execution_event_does_not_persist_stdout_by_default_but_updates_last_output_at(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake", attempt_id=uuid.uuid4())

    record_execution_event(superuser_db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="stdout", summary="some raw output line"))
    superuser_db.refresh(execution)
    assert execution.last_output_at is not None

    events = superuser_db.query(AgentWorkAssignmentEvent).filter_by(
        assignment_id=assignment.id, event_type=AgentWorkAssignmentEventType.execution_observed
    ).all()
    assert events == []  # ephemeral by default -- never silently persisted


@pytest.mark.asyncio
async def test_record_execution_event_persist_override_forces_a_stdout_chunk_durable(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake", attempt_id=uuid.uuid4())

    record_execution_event(superuser_db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="stdout", summary="final summary line"), persist=True)
    events = superuser_db.query(AgentWorkAssignmentEvent).filter_by(
        assignment_id=assignment.id, event_type=AgentWorkAssignmentEventType.execution_observed
    ).all()
    assert len(events) == 1
    assert events[0].detail["kind"] == "stdout"


def test_all_durable_event_kinds_are_a_fixed_closed_vocabulary():
    assert DURABLE_EVENT_KINDS == {"status", "progress", "tool_action", "heartbeat", "partial_result", "final_result"}


# ============================================================================ Requirement 2:
# the interactive control contract -- fail closed on every unsupported capability.

@pytest.mark.asyncio
async def test_send_execution_instruction_succeeds_against_a_capable_adapter(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-streaming", attempt_id=uuid.uuid4())
    adapter = _FakeStreamingAgentAdapter()

    outcome = await send_execution_instruction(superuser_db, execution=execution, assignment=assignment, adapter=adapter, instruction="focus on the auth module")
    assert outcome.outcome == OUTCOME_OK
    assert adapter.instructions == ["focus on the auth module"]


@pytest.mark.asyncio
async def test_send_execution_instruction_rejected_as_unsupported_against_local_cli_adapter(superuser_db, owner_id, tmp_path):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=uuid.uuid4())
    adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)

    outcome = await send_execution_instruction(superuser_db, execution=execution, assignment=assignment, adapter=adapter, instruction="ignored")
    assert outcome.outcome == OUTCOME_UNSUPPORTED_CAPABILITY
    # the adapter's own send_instruction() (which would raise NotImplementedError) was never
    # even called -- the capability check rejected it BEFORE that, exactly as documented.


@pytest.mark.asyncio
async def test_cancel_execution_rejected_as_unsupported_against_not_configured_adapter(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="codex", attempt_id=uuid.uuid4())
    adapter = NotConfiguredAdapter(provider_key="codex")

    outcome = await cancel_execution(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert outcome.outcome == OUTCOME_UNSUPPORTED_CAPABILITY


@pytest.mark.asyncio
async def test_cancel_execution_succeeds_and_transitions_assignment_to_cancelled(superuser_db, owner_id):
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    adapter = _FakeStreamingAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-streaming", attempt_id=decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)

    outcome = await cancel_execution(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert outcome.outcome == OUTCOME_OK
    assert adapter.cancelled is True
    superuser_db.refresh(assignment)
    assert assignment.status.value == "cancelled"
    superuser_db.refresh(execution)
    assert execution.adapter_state == ExecutionAdapterState.cancelled
    assert execution.ended_at is not None


@pytest.mark.asyncio
async def test_resume_execution_rejected_as_unsupported_then_succeeds_against_capable_adapter(superuser_db, owner_id, tmp_path):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=uuid.uuid4())
    local_adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    rejected = await resume_execution(superuser_db, execution=execution, assignment=assignment, adapter=local_adapter)
    assert rejected.outcome == OUTCOME_UNSUPPORTED_CAPABILITY

    streaming_adapter = _FakeStreamingAgentAdapter()
    accepted = await resume_execution(superuser_db, execution=execution, assignment=assignment, adapter=streaming_adapter)
    assert accepted.outcome == OUTCOME_OK
    assert streaming_adapter.resumed is True


@pytest.mark.asyncio
async def test_request_execution_status_is_never_capability_gated_but_still_surfaces_the_adapters_own_honest_refusal(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="codex", attempt_id=uuid.uuid4())
    with pytest.raises(ProviderNotConfiguredError):
        await request_execution_status(execution=execution, adapter=NotConfiguredAdapter(provider_key="codex"))

    streaming_adapter = _FakeStreamingAgentAdapter()
    await streaming_adapter.start_assignment(assignment.id)
    observation = await request_execution_status(execution=execution, adapter=streaming_adapter)
    assert observation.raw_status == "running"


# ============================================================================ Requirement 3:
# long-running process tracking.

@pytest.mark.asyncio
async def test_start_execution_tracking_creates_row_in_starting_state(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    attempt_id = uuid.uuid4()
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="claude-code", attempt_id=attempt_id)
    assert execution.adapter_state == ExecutionAdapterState.starting
    assert execution.result_ingestion_status == ExecutionResultIngestionStatus.pending
    assert execution.attempt_id == attempt_id
    assert execution.owner_id == owner_id
    assert execution.last_heartbeat_at is None
    assert execution.last_output_at is None
    assert execution.ended_at is None


# ============================================================================ Requirement 4:
# reconnect / recovery -- never a false COMPLETE.

@pytest.mark.asyncio
async def test_reconcile_execution_state_distinguishes_alive_from_exited_pending_ingestion(superuser_db, owner_id):
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    adapter = _FakeStreamingAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-streaming", attempt_id=decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)

    alive = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert alive.outcome == RECONCILE_PROCESS_ALIVE
    superuser_db.refresh(assignment)
    assert assignment.status.value != "completed"  # reconciliation alone never advances this

    await adapter.collect_result(assignment.id)  # the process itself now reports "exited"
    pending = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert pending.outcome == RECONCILE_RESULT_PENDING_INGESTION
    superuser_db.refresh(assignment)
    assert assignment.status.value != "completed"  # STILL never a false COMPLETE


@pytest.mark.asyncio
async def test_reconcile_execution_state_distinguishes_disconnected_adapter(superuser_db, owner_id):
    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="codex", attempt_id=uuid.uuid4())
    decision = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=NotConfiguredAdapter(provider_key="codex"))
    assert decision.outcome == RECONCILE_ADAPTER_DISCONNECTED


@pytest.mark.asyncio
async def test_reconcile_execution_state_distinguishes_session_lost(superuser_db, owner_id):
    """`LocalCLIAdapter.observe()` reports untracked ids as `raw_status="not_started"` (never
    raises) -- a fresh instance after a restart correctly reconciles as `RESULT_UNAVAILABLE`
    (proven separately below), not `SESSION_LOST`. This test instead uses a minimal, clearly
    labelled fake whose `observe()` genuinely raises `AdapterProcessLostError` -- modeling a
    richer future provider that CAN detect a torn-down session at observe-time (e.g. checking
    a session file/socket), proving `reconcile_execution_state()`'s own handling of that case
    without depending on `LocalCLIAdapter`'s specific (and equally valid) choice not to."""

    class _ObserveRaisesSessionLostAdapter:
        def control_capabilities(self):
            return AdapterCapabilities()

        async def observe(self, assignment_id):
            raise AdapterProcessLostError(f"no live session found for {assignment_id}")

    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=uuid.uuid4())
    mark_execution_running(superuser_db, execution=execution)
    decision = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=_ObserveRaisesSessionLostAdapter())
    assert decision.outcome == RECONCILE_SESSION_LOST
    superuser_db.refresh(execution)
    assert execution.adapter_state == ExecutionAdapterState.lost


@pytest.mark.asyncio
async def test_reconcile_execution_state_distinguishes_result_unavailable_after_a_restart(superuser_db, owner_id, tmp_path):
    """The realistic version of the same scenario: a FRESH `LocalCLIAdapter` instance (e.g.
    after a backend restart lost the in-memory process handle) genuinely reports
    `not_started` for an id it never tracked -- distinct from `SESSION_LOST` above, and
    exactly what `RECONCILE_RESULT_UNAVAILABLE` exists to name."""

    assignment, _agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=uuid.uuid4())
    fresh_adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    mark_execution_running(superuser_db, execution=execution)
    decision = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=fresh_adapter)
    assert decision.outcome == RECONCILE_RESULT_UNAVAILABLE


# ============================================================================ Requirement 5:
# founder-controlled credential/config interface -- reference only, never a secret.

def test_credential_reference_is_none_by_default_and_reflects_founder_config_when_set(monkeypatch):
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_CREDENTIAL_REF__CODEX", raising=False)
    assert credential_reference("codex") is None  # unresolved / config-required

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_CREDENTIAL_REF__CODEX", "vault:codex-oauth-label")
    assert credential_reference("codex") == "vault:codex-oauth-label"


def test_resolve_adapter_env_only_forwards_explicitly_allowlisted_names(monkeypatch):
    monkeypatch.setenv("SOME_HARMLESS_TEST_VAR", "harmless-value")
    monkeypatch.setenv("SOME_OTHER_TEST_VAR", "other-value")
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__CODEX", raising=False)
    assert resolve_adapter_env("codex") == {}  # no allowlist -- nothing forwarded

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__CODEX", "SOME_HARMLESS_TEST_VAR, NAME_THAT_IS_NOT_ACTUALLY_SET")
    resolved = resolve_adapter_env("codex")
    assert resolved == {"SOME_HARMLESS_TEST_VAR": "harmless-value"}
    assert "SOME_OTHER_TEST_VAR" not in resolved  # not allowlisted -- never blindly forwarded


# ============================================================================ Requirement 6:
# test adapter -- capability declarations proven against both the fake and a REAL (harmless)
# LocalCLIAdapter instance.

def test_local_cli_adapter_declares_only_cancel_capability(tmp_path):
    adapter = LocalCLIAdapter(provider_key="test-caps", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    assert adapter.control_capabilities() == AdapterCapabilities(supports_cancel=True)


def test_not_configured_adapter_declares_no_capabilities():
    assert NotConfiguredAdapter(provider_key="codex").control_capabilities() == AdapterCapabilities()


def test_fake_streaming_adapter_declares_full_capabilities():
    caps = _FakeStreamingAgentAdapter().control_capabilities()
    assert caps == AdapterCapabilities(supports_streaming=True, supports_instruction=True, supports_resume=True, supports_cancel=True, supports_structured_events=True)


# ============================================================================ Requirement 7:
# the full E2E control loop, plus lost-process (via a genuinely real subprocess mechanism) and
# timeout.

@pytest.mark.asyncio
async def test_full_interactive_control_loop_from_dispatch_through_result_ingestion(superuser_db, owner_id):
    # -- Life selects an assignment and dispatch is authorized.
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    adapter = _FakeStreamingAgentAdapter()
    dispatch_decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)
    assert dispatch_decision.outcome == "ASSIGNABLE"
    superuser_db.refresh(assignment)
    assert assignment.status.value == "running"

    # -- adapter starts, tracked.
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-streaming", attempt_id=dispatch_decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)
    assert execution.adapter_state == ExecutionAdapterState.running

    # -- output arrives (ephemeral by default) and a durable progress event is recorded.
    record_execution_event(superuser_db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="stdout", summary="compiling..."))
    record_execution_event(superuser_db, execution=execution, assignment=assignment, event=ExecutionEvent(kind="progress", summary="30% done"))
    superuser_db.refresh(execution)
    assert execution.last_output_at is not None
    assert execution.last_heartbeat_at is not None

    # -- status/heartbeat check via reconciliation: still alive.
    alive = await reconcile_execution_state(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert alive.outcome == RECONCILE_PROCESS_ALIVE

    # -- an instruction is sent successfully (this adapter supports it).
    instruction_outcome = await send_execution_instruction(superuser_db, execution=execution, assignment=assignment, adapter=adapter, instruction="run the test suite")
    assert instruction_outcome.outcome == OUTCOME_OK
    assert adapter.instructions == ["run the test suite"]

    # -- an instruction against a DIFFERENT, non-streaming real adapter is explicitly rejected
    # as unsupported -- proving fail-closed, not merely proving the happy path above.
    non_streaming = NotConfiguredAdapter(provider_key="claude-code")
    rejected = await send_execution_instruction(superuser_db, execution=execution, assignment=assignment, adapter=non_streaming, instruction="ignored")
    assert rejected.outcome == OUTCOME_UNSUPPORTED_CAPABILITY

    # -- the process completes; result is collected and ingested through the EXISTING dispatch
    # collection primitive, wrapped by execution-tracking bookkeeping.
    result_decision = await collect_and_ingest_execution_result(
        superuser_db, execution=execution, assignment=assignment, agent=agent, adapter=adapter, adapter_key="fake-streaming"
    )
    assert result_decision.outcome == "ASSIGNABLE"
    superuser_db.refresh(assignment)
    assert assignment.status.value == "completed"  # canonical assignment state updated
    superuser_db.refresh(execution)
    assert execution.adapter_state == ExecutionAdapterState.exited
    assert execution.result_ingestion_status == ExecutionResultIngestionStatus.ingested
    assert execution.ended_at is not None


@pytest.mark.asyncio
async def test_e2e_control_loop_lost_process_during_collection_never_falsely_reports_completed(superuser_db, owner_id, tmp_path):
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    start_adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=start_adapter)
    assert decision.outcome == "ASSIGNABLE"
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)

    # A FRESH adapter instance for collection -- its own `_processes` dict never saw this
    # assignment's process started, so collect_result() genuinely raises AdapterProcessLostError.
    fresh_adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    result_decision = await collect_and_ingest_execution_result(
        superuser_db, execution=execution, assignment=assignment, agent=agent, adapter=fresh_adapter, adapter_key="cursor-agent"
    )
    assert result_decision.outcome == "ADAPTER_PROCESS_LOST"
    superuser_db.refresh(assignment)
    assert assignment.status.value == "blocked"
    assert assignment.status.value != "completed"
    superuser_db.refresh(execution)
    assert execution.adapter_state == ExecutionAdapterState.lost
    assert execution.result_ingestion_status == ExecutionResultIngestionStatus.failed


@pytest.mark.asyncio
async def test_e2e_control_loop_timeout_during_collection_never_falsely_reports_completed(superuser_db, owner_id, tmp_path):
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/bin/sleep", args_template=("5",), cwd=str(tmp_path), timeout_seconds=1)
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)
    assert decision.outcome == "ASSIGNABLE"
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cursor-agent", attempt_id=decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)

    result_decision = await collect_and_ingest_execution_result(
        superuser_db, execution=execution, assignment=assignment, agent=agent, adapter=adapter, adapter_key="cursor-agent"
    )
    assert result_decision.outcome == OUTCOME_ADAPTER_TIMEOUT
    superuser_db.refresh(assignment)
    assert assignment.status.value == "blocked"
    superuser_db.refresh(execution)
    assert execution.adapter_state == ExecutionAdapterState.timeout
    assert execution.result_ingestion_status == ExecutionResultIngestionStatus.failed


@pytest.mark.asyncio
async def test_e2e_control_loop_cancellation_reaches_a_terminal_assignment_state_not_completed(superuser_db, owner_id):
    assignment, agent = _ready_dispatchable_assignment(superuser_db, owner_id)
    adapter = _FakeStreamingAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-streaming", attempt_id=decision.attempt_id)
    mark_execution_running(superuser_db, execution=execution)

    outcome = await cancel_execution(superuser_db, execution=execution, assignment=assignment, adapter=adapter)
    assert outcome.outcome == OUTCOME_OK
    superuser_db.refresh(assignment)
    assert assignment.status.value == "cancelled"
    assert assignment.status.value != "completed"
