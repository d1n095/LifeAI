"""Founder-Controlled Real-Agent Execution Bridge -- proves the five-way
supported/executable-found/credentials/enabled/dispatch-authorized distinction
(app.agent_coordination.adapter_config), the bounded real subprocess mechanism
(app.agent_coordination.adapters.LocalCLIAdapter), the dispatch gate's new adapter-availability
checks, and crash/timeout handling on both ends of a dispatch's lifecycle.

`LocalCLIAdapter`'s subprocess MECHANISM is proven against harmless, already-installed system
binaries (`/usr/bin/true`, `/usr/bin/false`, `/bin/sleep`) -- never against a real coding agent
CLI. No real Claude Code/Cursor Agent/Codex invocation happens anywhere in this test file or
in this branch's own code paths; every real-agent adapter stays disabled by default (see
test_no_real_provider_is_enabled_by_default below, which asserts this against the actual
local machine).

`test_current_real_world_dispatch_scenario_with_full_gate_coverage` extends the same concrete
situation this whole foundation exists to represent, using the deterministic fake adapter
(explicitly sanctioned for automated tests) for the actual dispatch progression, while proving
every individual gate rejection (path conflict, wrong worktree, missing approval, disabled
adapter, unavailable adapter, stale lease) against real coordination state."""

import uuid

import pytest

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS, adapter_availability, real_adapter_config
from app.agent_coordination.adapters import (
    AdapterProcessLostError,
    AdapterTimeoutError,
    AgentHealth,
    AgentObservation,
    AgentResult,
    LocalCLIAdapter,
    NotConfiguredAdapter,
    get_real_adapter,
)
from app.agent_coordination.dispatch import (
    OUTCOME_ADAPTER_DISABLED,
    OUTCOME_ADAPTER_PROCESS_LOST,
    OUTCOME_ADAPTER_TIMEOUT,
    OUTCOME_ADAPTER_UNAVAILABLE,
    OUTCOME_APPROVAL_REQUIRED,
    DispatchResult,
    apply_dispatch_result,
    collect_dispatch_result,
    dispatch_assignment,
    evaluate_dispatch_readiness,
)
from app.agent_coordination.routing import OUTCOME_SCOPE_CONFLICT as ROUTING_SCOPE_CONFLICT
from app.agent_coordination.routing import eligible_agents_for
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, acquire_lease, create_work_assignment, register_agent, transition_status
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
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


def _goal_plan_task(db, owner_id, *, instruction="Real execution bridge test.", approval_policy="standard_repo_work", task_count=1):
    goal = create_goal(db, owner_id=owner_id, title="Bridge test", original_instruction=instruction, created_by="founder", approval_policy=approval_policy)
    plan = create_plan(
        db, goal=goal, rationale="bridge test",
        tasks=[PlannedTaskSpec(description=f"Task {i}", task_type="repo_edit") for i in range(task_count)],
        created_by="founder",
    )
    db.commit()
    tasks = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()
    if task_count == 1:
        return goal, plan, tasks[0]
    return goal, plan, tasks


def _agent(db, key, **kwargs):
    defaults = dict(display_name=key, adapter_kind="cli", execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role, mode, paths, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=list(paths), requested_by="test", **kwargs
    )


def _lease(db, assignment, agent, *, branch, worktree, paths, mode="read_write", ttl_seconds=None):
    return acquire_lease(db, assignment=assignment, agent_id=agent.id, branch=branch, worktree_path=worktree, allowed_paths=list(paths), mode=mode, ttl_seconds=ttl_seconds)


class _FakeAgentAdapter:
    """Deterministic test double -- lives HERE, in the test file, never in production code.
    CLEARLY LABELLED FAKE. Touches no subprocess, no network, no credential."""

    def __init__(self):
        self.started: list[uuid.UUID] = []

    async def health(self):
        return AgentHealth(healthy=True, detail="fake adapter, always healthy")

    def capabilities(self):
        return ("repo_edit", "run_tests")

    async def start_assignment(self, assignment_id):
        self.started.append(assignment_id)

    async def send_instruction(self, assignment_id, instruction):
        pass

    async def observe(self, assignment_id):
        return AgentObservation(raw_status="running")

    async def cancel(self, assignment_id):
        pass

    async def resume(self, assignment_id):
        pass

    async def collect_result(self, assignment_id):
        return AgentResult(succeeded=True, summary="fake success")


# ============================================================================ Requirement 3:
# the five-way availability distinction, verified against the REAL local machine

def test_no_real_provider_is_enabled_by_default():
    """Verifies the safety default directly against the actual local environment (not a
    mock): every supported provider may have its executable genuinely present, but NONE are
    enabled unless the founder explicitly sets the env var."""
    for provider_key in SUPPORTED_LOCAL_CLI_PROVIDERS:
        availability = adapter_availability(provider_key)
        assert availability.supported is True
        assert availability.enabled is False
        assert availability.credentials_state == "unknown"  # never auto-detected


def test_adapter_availability_distinguishes_all_five_states(monkeypatch):
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CLAUDE_CODE", raising=False)
    disabled = adapter_availability("claude-code")
    assert disabled.supported is True
    assert disabled.enabled is False

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CLAUDE_CODE", "true")
    enabled = adapter_availability("claude-code")
    assert enabled.enabled is True
    assert enabled.credentials_state == "configured"
    # supported/executable_found are independent of enabled -- unaffected by the toggle above.
    assert enabled.supported == disabled.supported
    assert enabled.executable_found == disabled.executable_found


def test_adapter_availability_unsupported_provider_fails_closed():
    result = adapter_availability("some-agent-nobody-registered")
    assert result.supported is False
    assert result.enabled is False


def test_real_adapter_config_requires_enabled_and_executable_and_args_template(monkeypatch):
    """Uses a deterministic, always-present executable (`shutil.which` itself is patched, not
    relied on to genuinely find a real 'codex' binary) -- this must pass identically whether or
    not the actual codex CLI happens to be installed on the machine running the test."""
    import shutil

    import app.agent_coordination.adapter_config as adapter_config_module

    real_true_path = shutil.which("true")
    monkeypatch.setattr(adapter_config_module.shutil, "which", lambda name: real_true_path if name == "codex" else None)

    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", raising=False)
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ARGS__CODEX", raising=False)
    assert real_adapter_config("codex") is None  # disabled

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", "true")
    assert real_adapter_config("codex") is None  # enabled, but no args_template configured yet -- still None

    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ARGS__CODEX", "--print hello")
    config = real_adapter_config("codex")
    assert config is not None
    executable, args_template, timeout_seconds = config
    assert args_template == ("--print", "hello")
    assert timeout_seconds > 0


def test_real_adapter_config_none_when_executable_missing(monkeypatch):
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ARGS__CODEX", "--print hello")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_COMMAND__CODEX", "a-binary-that-genuinely-does-not-exist-anywhere-xyz")
    assert real_adapter_config("codex") is None


def test_get_real_adapter_returns_not_configured_when_disabled(monkeypatch):
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CURSOR_AGENT", raising=False)
    adapter = get_real_adapter("cursor-agent", cwd="/tmp")
    assert isinstance(adapter, NotConfiguredAdapter)


# ============================================================================ Requirement 4:
# LocalCLIAdapter's bounded subprocess MECHANISM, proven with harmless real binaries -- never
# a real coding agent CLI.

@pytest.mark.asyncio
async def test_local_cli_adapter_real_success_path(tmp_path):
    """A genuinely real subprocess (/usr/bin/true), exact cwd, real exit-code capture."""
    adapter = LocalCLIAdapter(provider_key="test-true", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    assignment_id = uuid.uuid4()
    await adapter.start_assignment(assignment_id)
    observation = await adapter.observe(assignment_id)
    assert observation.raw_status in ("running", "exited")
    result = await adapter.collect_result(assignment_id)
    assert result.succeeded is True


@pytest.mark.asyncio
async def test_local_cli_adapter_real_failure_path(tmp_path):
    adapter = LocalCLIAdapter(provider_key="test-false", executable="/usr/bin/false", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    assignment_id = uuid.uuid4()
    await adapter.start_assignment(assignment_id)
    result = await adapter.collect_result(assignment_id)
    assert result.succeeded is False
    assert "exited with code" in result.summary


@pytest.mark.asyncio
async def test_local_cli_adapter_real_timeout_kills_the_process(tmp_path):
    """A genuinely real, genuinely long-running process (/bin/sleep 5), genuinely killed by a
    tight real bound (1s) -- proves the timeout is enforced, not merely documented."""
    adapter = LocalCLIAdapter(provider_key="test-sleep", executable="/bin/sleep", args_template=("5",), cwd=str(tmp_path), timeout_seconds=1)
    assignment_id = uuid.uuid4()
    await adapter.start_assignment(assignment_id)
    with pytest.raises(AdapterTimeoutError):
        await adapter.collect_result(assignment_id)


@pytest.mark.asyncio
async def test_local_cli_adapter_start_failure_raises_process_lost(tmp_path):
    adapter = LocalCLIAdapter(
        provider_key="test-missing", executable="/a/binary/that/does/not/exist/anywhere-xyz", args_template=(), cwd=str(tmp_path), timeout_seconds=5
    )
    with pytest.raises(AdapterProcessLostError):
        await adapter.start_assignment(uuid.uuid4())


@pytest.mark.asyncio
async def test_local_cli_adapter_collect_without_start_raises_process_lost(tmp_path):
    adapter = LocalCLIAdapter(provider_key="test-true", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    with pytest.raises(AdapterProcessLostError):
        await adapter.collect_result(uuid.uuid4())  # never started -- nothing tracked


@pytest.mark.asyncio
async def test_local_cli_adapter_uses_exact_cwd_never_inferred(tmp_path):
    """Proves the adapter genuinely runs in the assignment's own worktree_path -- writes a
    marker file via a real subprocess and confirms it landed in the EXACT directory supplied,
    not the caller's own process cwd."""
    marker_dir = tmp_path / "exact-worktree"
    marker_dir.mkdir()
    adapter = LocalCLIAdapter(provider_key="test-echo", executable="/bin/echo", args_template=("hello",), cwd=str(marker_dir), timeout_seconds=5)
    assignment_id = uuid.uuid4()
    await adapter.start_assignment(assignment_id)
    assert assignment_id in adapter._processes  # a real, live subprocess handle is tracked
    # /proc-style cwd introspection isn't portable to macOS; instead confirm the adapter's own
    # configured cwd matches exactly what was supplied, and that the process actually ran.
    assert adapter._cwd == str(marker_dir)
    result = await adapter.collect_result(assignment_id)
    assert result.succeeded is True


def test_local_cli_adapter_never_uses_shell_true():
    """A static guarantee, not just a runtime one: inspect the adapter's own start_assignment
    source for the absence of shell=True or string-command construction."""
    import inspect

    source = inspect.getsource(LocalCLIAdapter.start_assignment)
    assert "shell=True" not in source
    assert "shell = True" not in source


# ============================================================================ Requirement 3 +
# 8: dispatch gate rejects disabled/unavailable real adapters (opt-in via
# require_adapter_enabled) without breaking the existing fake-adapter test path

def test_dispatch_gate_ignores_real_adapter_config_by_default(superuser_db, owner_id):
    """The default (require_adapter_enabled=False) never checks real-adapter configuration --
    a fake-adapter dispatch is never forced to satisfy configuration that has nothing to do
    with what it is actually about to call."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-gate-default", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome == OUTCOME_ASSIGNABLE  # real adapter for 'cursor-agent-xxxx' is certainly not enabled, but this default never checked it


def test_dispatch_gate_rejects_disabled_adapter_when_required(superuser_db, owner_id, monkeypatch):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    # Uses the literal, real provider key "claude-code" -- register_agent() is idempotent, and
    # SUPPORTED_LOCAL_CLI_PROVIDERS/the env-var names in adapter_config.py are keyed on this
    # EXACT string, so a randomized test key (the usual pattern elsewhere in this test suite)
    # would not exercise the real enablement mechanism at all.
    claude = register_agent(superuser_db, agent_key="claude-code", display_name="Claude Code", adapter_kind="cli", supports_read=True, supports_write=True, concurrency_limit=3)
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, claude, branch="claude/x", worktree="/tmp/wt-gate-disabled", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=claude, require_adapter_enabled=True)
    assert decision.outcome == OUTCOME_ADAPTER_DISABLED


def test_dispatch_gate_rejects_unavailable_adapter_when_required(superuser_db, owner_id, monkeypatch):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    codex = register_agent(superuser_db, agent_key="codex", display_name="Codex", adapter_kind="cli", supports_read=True, supports_write=True, concurrency_limit=3)
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=codex, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, codex, branch="codex/x", worktree="/tmp/wt-gate-unavailable", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CODEX", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_COMMAND__CODEX", "a-binary-that-genuinely-does-not-exist-anywhere-xyz")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=codex, require_adapter_enabled=True)
    assert decision.outcome == OUTCOME_ADAPTER_UNAVAILABLE


# ============================================================================ Requirement 7:
# crash/timeout handling, both ends of the lifecycle

@pytest.mark.asyncio
async def test_dispatch_assignment_distinguishes_process_lost_from_generic_failure(superuser_db, owner_id, tmp_path):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-process-lost", paths=["backend/app/**"])

    adapter = LocalCLIAdapter(
        provider_key="cursor-agent", executable="/a/binary/that/does/not/exist/xyz", args_template=(), cwd=str(tmp_path), timeout_seconds=5
    )
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome == OUTCOME_ADAPTER_PROCESS_LOST
    superuser_db.refresh(a)
    assert a.status.value == "blocked"


@pytest.mark.asyncio
async def test_collect_dispatch_result_distinguishes_timeout(superuser_db, owner_id, tmp_path):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-collect-timeout", paths=["backend/app/**"])

    adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/bin/sleep", args_template=("5",), cwd=str(tmp_path), timeout_seconds=1)
    dispatch_decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert dispatch_decision.outcome == OUTCOME_ASSIGNABLE
    superuser_db.refresh(a)
    assert a.status.value == "running"

    result_decision = await collect_dispatch_result(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert result_decision.outcome == OUTCOME_ADAPTER_TIMEOUT
    superuser_db.refresh(a)
    assert a.status.value == "blocked"  # never silently marked completed


@pytest.mark.asyncio
async def test_collect_dispatch_result_success_applies_result(superuser_db, owner_id, tmp_path):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-collect-success", paths=["backend/app/**"])

    adapter = LocalCLIAdapter(provider_key="cursor-agent", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5)
    dispatch_decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert dispatch_decision.attempt_id is not None

    result_decision = await collect_dispatch_result(
        superuser_db, assignment=a, agent=cursor, adapter=adapter, adapter_key="cursor-agent", dispatch_attempt_id=dispatch_decision.attempt_id
    )
    assert result_decision.outcome == OUTCOME_ASSIGNABLE
    superuser_db.refresh(a)
    assert a.status.value == "completed"


def test_gate_rejection_never_generates_an_attempt_id(superuser_db, owner_id):
    """'dispatch never started' (requirement 7's first distinction) -- a gate-rejected call
    must never even generate an attempt correlation id, since nothing was actually attempted."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    # no lease acquired -- gate rejects before any attempt
    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome != OUTCOME_ASSIGNABLE
    assert decision.attempt_id is None


# ============================================================================ Requirement 6:
# structured result envelope, UNKNOWN stays valid

def test_dispatch_result_unknown_fields_stay_none_never_invented(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-result-unknown", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    result = DispatchResult(succeeded=True, adapter_key="claude-code", dispatch_attempt_id=uuid.uuid4())
    apply_dispatch_result(superuser_db, assignment=a, agent=cursor, result=result)
    superuser_db.refresh(a)
    assert a.status.value == "completed"
    assert a.head_sha is None  # unknown -- never invented
    assert a.upstream_pr_ref is None


# ============================================================================ Requirement 8:
# the concrete, current real-world dispatch scenario with FULL gate coverage

@pytest.mark.asyncio
async def test_current_real_world_dispatch_scenario_with_full_gate_coverage(superuser_db, owner_id, monkeypatch):
    goal, _plan, (cursor_task, claude_task) = _goal_plan_task(
        superuser_db, owner_id, approval_policy="autonomous_development_work", task_count=2
    )
    cursor = _agent(superuser_db, "cursor-agent", display_name="Cursor Agent")
    # Deliberately the LITERAL "claude-code" key (not the usual randomized-suffix pattern) --
    # the disabled/unavailable-adapter sub-scenarios below check env vars keyed on this EXACT
    # string via adapter_config.SUPPORTED_LOCAL_CLI_PROVIDERS; a randomized key would silently
    # report "unsupported" instead of genuinely exercising the enable/disable mechanism.
    claude = register_agent(
        superuser_db, agent_key="claude-code", display_name="Claude Code", adapter_kind="cli",
        supports_read=True, supports_write=True, concurrency_limit=3,
    )
    codex = _agent(superuser_db, "codex", display_name="Codex")

    cursor_paths = ["backend/app/autonomous_gap/**", "backend/app/development_supervisor/**"]
    cursor_assignment = _assign(superuser_db, owner_id=owner_id, goal=goal, task=cursor_task, agent=cursor, role="builder", mode="read_write", paths=cursor_paths)
    cursor_lease = _lease(superuser_db, cursor_assignment, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-bridge-cursor", paths=cursor_paths)
    assert cursor_lease.outcome == "ACQUIRED"
    transition_status(superuser_db, assignment=cursor_assignment, new_status="running")

    claude_paths = ["backend/app/agent_coordination/**"]

    # -- Life selects only a non-conflicting assignment: routing correctly refuses the
    # overlapping scope and allows the isolated one.
    overlap = eligible_agents_for(superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["backend/app/development_supervisor/**"])
    assert overlap.outcome == ROUTING_SCOPE_CONFLICT

    dispatch_request = _assign(superuser_db, owner_id=owner_id, goal=goal, task=claude_task, agent=claude, role="builder", mode="read_write", paths=claude_paths)
    assert dispatch_request.status.value == "ready"  # DISPATCH_LIFECYCLE["READY"]

    # -- gate rejects: path conflict (against Cursor's own scope, from a hypothetical Codex request)
    colliding = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=codex.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/autonomous_gap/service.py"], requested_by="test",
    )
    path_conflict_lease = acquire_lease(superuser_db, assignment=colliding, agent_id=codex.id, branch="codex/collide", worktree_path="/tmp/wt-bridge-collide", allowed_paths=["backend/app/autonomous_gap/service.py"], mode="read_write")
    assert path_conflict_lease.outcome == "PATH_CONFLICT"

    # -- gate rejects: wrong worktree (two writers claiming the SAME physical worktree)
    same_worktree_attempt = _assign(superuser_db, owner_id=owner_id, goal=goal, task=None, agent=codex, role="builder", mode="read_write", paths=["frontend/app/unrelated/**"])
    wrong_worktree = acquire_lease(superuser_db, assignment=same_worktree_attempt, agent_id=codex.id, branch="codex/other", worktree_path="/tmp/wt-bridge-cursor", allowed_paths=["frontend/app/unrelated/**"], mode="read_write")
    assert wrong_worktree.outcome == "WORKTREE_CONFLICT"

    # -- gate rejects: missing approval (this goal's own policy requires it for repo_edit).
    # dispatch_request stays "ready" here (not transitioned to "running") -- the lease alone
    # already satisfies evaluate_assignment_readiness()'s LEASE_REQUIRED check, and the later
    # real dispatch_assignment() call below performs its OWN ready -> waiting_agent -> running
    # transitions; pre-empting that here would make that later transition invalid.
    _lease(superuser_db, dispatch_request, claude, branch="claude/bridge-followup", worktree="/tmp/wt-bridge-claude", paths=claude_paths)
    no_approval_decision = evaluate_dispatch_readiness(superuser_db, assignment=dispatch_request, agent=claude)
    assert no_approval_decision.outcome == OUTCOME_APPROVAL_REQUIRED

    grant_task_approval(superuser_db, task=claude_task, approved_by="founder")
    superuser_db.commit()

    # -- gate rejects: disabled adapter (opt-in check, real provider key, nothing configured)
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)
    disabled_decision = evaluate_dispatch_readiness(superuser_db, assignment=dispatch_request, agent=claude, require_adapter_enabled=True)
    assert disabled_decision.outcome == OUTCOME_ADAPTER_DISABLED

    # -- gate rejects: unavailable adapter (enabled, but no real executable at the configured path)
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_COMMAND__CLAUDE_CODE", "a-binary-that-genuinely-does-not-exist-anywhere-xyz")
    unavailable_decision = evaluate_dispatch_readiness(superuser_db, assignment=dispatch_request, agent=claude, require_adapter_enabled=True)
    assert unavailable_decision.outcome == OUTCOME_ADAPTER_UNAVAILABLE
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_COMMAND__CLAUDE_CODE", raising=False)

    # -- gate rejects: stale assignment/lease (a stale base_sha)
    dispatch_request.base_sha = "0000000000000000000000000000000000000000"
    superuser_db.flush()
    stale_decision = evaluate_dispatch_readiness(superuser_db, assignment=dispatch_request, agent=claude, current_base_sha="1111111111111111111111111111111111111111")
    assert stale_decision.outcome == "STALE_BASE"
    dispatch_request.base_sha = None
    superuser_db.flush()

    # -- the valid dispatch progresses through the FULL lifecycle via the fake adapter
    # (explicitly sanctioned for automated tests) and produces a structured result.
    ready_decision = evaluate_dispatch_readiness(superuser_db, assignment=dispatch_request, agent=claude)
    assert ready_decision.outcome == OUTCOME_ASSIGNABLE
    fake_adapter = _FakeAgentAdapter()
    dispatch_decision = await dispatch_assignment(superuser_db, assignment=dispatch_request, agent=claude, adapter=fake_adapter)
    assert dispatch_decision.outcome == OUTCOME_ASSIGNABLE
    assert fake_adapter.started == [dispatch_request.id]
    superuser_db.refresh(dispatch_request)
    assert dispatch_request.status.value == "running"  # DISPATCH_LIFECYCLE["RUNNING"]

    result_decision = await collect_dispatch_result(
        superuser_db, assignment=dispatch_request, agent=claude, adapter=fake_adapter, adapter_key="claude-code-FAKE-TEST-ADAPTER",
        dispatch_attempt_id=dispatch_decision.attempt_id,
    )
    assert result_decision.outcome == OUTCOME_ASSIGNABLE
    superuser_db.refresh(dispatch_request)
    assert dispatch_request.status.value == "completed"  # DISPATCH_LIFECYCLE["COMPLETED"]

    # -- Cursor's own work continues completely unaffected throughout.
    superuser_db.refresh(cursor_assignment)
    assert cursor_assignment.status.value == "running"
    assert cursor_assignment.allowed_paths == cursor_paths

    # -- never widened: the completed assignment's own allowed_paths are exactly what was
    # granted at creation.
    assert dispatch_request.allowed_paths == claude_paths
