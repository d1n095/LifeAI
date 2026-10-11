"""Agent conversation turns: instruction delivery, answer capture, same-session continuation,
failures and authorization boundaries (app.agent_coordination.adapters dialects +
app.agent_coordination.session).

No real agent is ever launched: every "CLI" here is a small fake executable written to
tmp_path that emulates the documented output shape of `claude -p --output-format json`,
`cursor-agent -p --output-format json` or `codex exec --json`, and logs the exact argv it
received so the tests can assert what MainAI actually sends."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from app.agent_coordination import adapter_config
from app.agent_coordination.adapters import (
    AdapterInstructionError,
    AdapterSessionUnknownError,
    AdapterTimeoutError,
    ClaudeCodeDialect,
    CodexDialect,
    CursorAgentDialect,
    LocalCLIAdapter,
    NotConfiguredAdapter,
    build_prompt,
    get_real_adapter,
    validate_extra_args,
)
from app.agent_coordination.dispatch import OUTCOME_INSTRUCTION_REJECTED
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, create_work_assignment, register_agent, transition_status
from app.agent_coordination.session import (
    OUTCOME_ADAPTER_DISABLED,
    OUTCOME_NOT_AWAITING_INSTRUCTION,
    OUTCOME_SESSION_EXISTS,
    OUTCOME_SESSION_UNKNOWN,
    AgentSessionError,
    close_agent_session,
    continue_agent_session,
    recorded_session_id,
    run_agent_assignment_task,
    start_agent_session,
)
from app.mainai_execution.approval import requires_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentDispatchExecution, AgentWorkAssignmentEvent
from app.models.mainai_execution import MainAITask

SESSION = "sess-0001abcd"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


# ============================================================================ fake agent CLIs

_FAKE = r'''
import json, os, sys, time
MODE = {mode!r}
LOG = {log!r}
argv = sys.argv[1:]
with open(LOG, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "env": sorted(os.environ)}}) + "\n")
behaviour = os.path.join(os.path.dirname(LOG), "behaviour.json")
spec = json.load(open(behaviour)) if os.path.exists(behaviour) else {{}}
if spec.get("sleep"):
    time.sleep(spec["sleep"])
if spec.get("exit"):
    sys.stderr.write(spec.get("stderr", "boom"))
    sys.exit(spec["exit"])
resumed = "--resume" in argv or (len(argv) > 1 and argv[1] == "resume")
answer = spec.get("answer") or ("second answer" if resumed else "first answer")
session = spec.get("session", "{session}")
if MODE == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": answer}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
else:
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": answer,
                       "session_id": session, "num_turns": 1, "total_cost_usd": 0.01}}))
'''


def _fake_cli(tmp_path: Path, mode: str) -> tuple[str, Path]:
    log = tmp_path / f"{mode}-argv.log"
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(_FAKE.format(mode=mode, log=str(log), session=SESSION))
    exe = tmp_path / f"fake-{mode}"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(0o755)
    return str(exe), log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _behave(tmp_path: Path, **spec) -> None:
    (tmp_path / "behaviour.json").write_text(json.dumps(spec))


def _adapter(tmp_path, mode="claude", *, read_only=True, timeout=30, args=("--model", "sonnet")):
    exe, log = _fake_cli(tmp_path, mode)
    dialect = {"claude": ClaudeCodeDialect(), "cursor": CursorAgentDialect(), "codex": CodexDialect()}[mode]
    key = {"claude": "claude-code", "cursor": "cursor-agent", "codex": "codex"}[mode]
    adapter = LocalCLIAdapter(
        provider_key=key, executable=exe, args_template=args, cwd=str(tmp_path), timeout_seconds=timeout, env={},
        dialect=dialect, read_only=read_only, max_budget_usd=1.0 if mode == "claude" else None,
    )
    return adapter, log, key


# ============================================================================ adapter: delivery,
# capture, continuation (no database)

@pytest.mark.asyncio
async def test_claude_turn_delivers_the_real_instruction_and_captures_answer_and_session(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="Summarize backend/app/agent_coordination.")
    result = await adapter.collect_result(aid)

    argv = _calls(log)[0]["argv"]
    assert argv[:8] == ["-p", "--output-format", "json", "--permission-mode", "plan", "--max-budget-usd", "1.00", "--model"]
    assert argv[-1] == build_prompt(aid, "Summarize backend/app/agent_coordination.")
    assert "Summarize backend/app/agent_coordination." in argv[-1] and not argv[-1].startswith("-")
    assert result.succeeded and result.final_message == "first answer" and result.session_id == SESSION
    kinds = {ref["kind"] for ref in result.output_refs}
    assert kinds == {"agent_final_message", "agent_session"}
    assert next(r for r in result.output_refs if r["kind"] == "agent_final_message")["self_report"] is True


@pytest.mark.asyncio
async def test_send_instruction_continues_the_same_session_by_explicit_id(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="first")
    await adapter.collect_result(aid)
    await adapter.send_instruction(aid, "now list the risks")
    second = await adapter.collect_result(aid)

    argv = _calls(log)[1]["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert "--continue" not in argv and "-c" not in argv
    assert second.final_message == "second answer" and second.session_id == SESSION


@pytest.mark.asyncio
async def test_send_instruction_without_a_mainai_created_session_is_refused(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude")
    with pytest.raises(AdapterSessionUnknownError):
        await adapter.send_instruction(uuid.uuid4(), "continue whatever you were doing")
    assert _calls(log) == []


@pytest.mark.asyncio
async def test_write_mode_uses_accept_edits_never_bypass(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude", read_only=False)
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="edit")
    await adapter.collect_result(aid)
    argv = _calls(log)[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in argv and "--dangerously-skip-permissions" not in argv


@pytest.mark.asyncio
async def test_cursor_turn_uses_fixed_sandbox_and_plan_mode(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "cursor")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="review")
    result = await adapter.collect_result(aid)
    argv = _calls(log)[0]["argv"]
    assert argv[argv.index("--sandbox") + 1] == "enabled"
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--force" not in argv and "--yolo" not in argv
    assert result.succeeded and result.session_id == SESSION

    await adapter.send_instruction(aid, "more")
    await adapter.collect_result(aid)
    resume_argv = _calls(log)[1]["argv"]
    assert resume_argv[resume_argv.index("--resume") + 1] == SESSION


@pytest.mark.asyncio
async def test_codex_dialect_argv_and_jsonl_parsing_with_a_fake_executable(tmp_path):
    """A FAKE codex executable only -- no real Codex session is launched or consumed."""
    adapter, log, _ = _adapter(tmp_path, "codex")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="inspect")
    result = await adapter.collect_result(aid)
    argv = _calls(log)[0]["argv"]
    assert argv[:2] == ["exec", "--json"]
    assert 'sandbox_mode="read-only"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv and "danger-full-access" not in " ".join(argv)
    assert result.succeeded and result.final_message == "first answer" and result.session_id == SESSION
    assert result.structured["usage"] == {"input_tokens": 10, "output_tokens": 5}

    await adapter.send_instruction(aid, "continue")
    await adapter.collect_result(aid)
    resume_argv = _calls(log)[1]["argv"]
    assert resume_argv[:3] == ["exec", "resume", "--json"]
    assert resume_argv[-2] == SESSION


@pytest.mark.parametrize("bad", ["", "   ", "has\x00nul", "x" * 32_001])
def test_instruction_validation_rejects_bad_input(bad):
    with pytest.raises(AdapterInstructionError):
        build_prompt(uuid.uuid4(), bad)


def test_an_instruction_starting_with_a_dash_can_never_become_an_option():
    prompt = build_prompt(uuid.uuid4(), "--dangerously-skip-permissions")
    assert prompt.startswith("MainAI assignment ")


@pytest.mark.parametrize("args", [
    ("--dangerously-skip-permissions",),
    ("--permission-mode", "bypassPermissions"),
    ("--force", "x"),
    ("--model", "--yolo"),
    ("--model",),
])
def test_args_template_may_only_select_a_model(args):
    with pytest.raises(AdapterInstructionError):
        validate_extra_args(args)


@pytest.mark.asyncio
async def test_agent_reported_malformed_session_id_is_never_recorded(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude")
    _behave(tmp_path, session="--resume-me")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="x")
    result = await adapter.collect_result(aid)
    assert result.session_id is None
    assert "agent_session" not in {r["kind"] for r in result.output_refs}
    with pytest.raises(AdapterSessionUnknownError):
        await adapter.send_instruction(aid, "y")


@pytest.mark.asyncio
async def test_failed_turn_is_not_success_and_keeps_bounded_redacted_diagnostics(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude")
    _behave(tmp_path, exit=3, stderr="token=supersecretvalue " + "e" * 5000)
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="x")
    result = await adapter.collect_result(aid)
    assert not result.succeeded
    failure = next(r for r in result.output_refs if r["kind"] == "agent_failure")
    assert failure["returncode"] == 3 and len(failure["stderr_tail"]) <= 2000
    assert "supersecretvalue" not in json.dumps(result.output_refs)


@pytest.mark.asyncio
async def test_secret_in_the_agent_answer_is_redacted(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude")
    _behave(tmp_path, answer="done; api_key=abcdef123456 stays private")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="x")
    result = await adapter.collect_result(aid)
    assert "abcdef123456" not in result.final_message


@pytest.mark.asyncio
async def test_timeout_kills_the_turn(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude", timeout=1)
    _behave(tmp_path, sleep=5)
    aid = uuid.uuid4()
    await adapter.start_assignment(aid, instruction="x")
    with pytest.raises(AdapterTimeoutError):
        await adapter.collect_result(aid)


@pytest.mark.asyncio
async def test_adapter_without_a_dialect_keeps_its_single_shot_behaviour(tmp_path):
    exe, _log = _fake_cli(tmp_path, "claude")
    adapter = LocalCLIAdapter(provider_key="legacy", executable=exe, args_template=("--x",), cwd=str(tmp_path), timeout_seconds=30, env={})
    assert not adapter.control_capabilities().supports_instruction
    with pytest.raises(AdapterInstructionError):
        await adapter.start_assignment(uuid.uuid4(), instruction="x")
    aid = uuid.uuid4()
    await adapter.start_assignment(aid)
    result = await adapter.collect_result(aid)
    assert {r["kind"] for r in result.output_refs} == {"stdout_bytes", "stderr_bytes"} and result.final_message is None


# ============================================================================ factory: Founder
# enablement, configuration validation, budget ceiling

def _enable(monkeypatch, tmp_path, key="CLAUDE_CODE", args="--model sonnet", budget=None, provider="claude"):
    exe, _log = _fake_cli(tmp_path, provider)
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_ENABLED__{key}", "true")
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_COMMAND__{key}", exe)
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_ARGS__{key}", args)
    if budget is not None:
        monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_MAX_BUDGET_USD__{key}", budget)


def test_factory_is_inert_until_the_founder_enables_it(monkeypatch, tmp_path):
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


def test_factory_builds_a_conversational_adapter_when_enabled(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    adapter = get_real_adapter("claude-code", cwd=str(tmp_path))
    assert isinstance(adapter, LocalCLIAdapter) and adapter.control_capabilities().supports_instruction


def test_factory_refuses_an_unsafe_args_template(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, args="--dangerously-skip-permissions")
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


@pytest.mark.parametrize("budget", ["0", "-1", "abc", str(adapter_config.HARD_MAX_BUDGET_USD + 1)])
def test_factory_refuses_an_invalid_or_excessive_budget(monkeypatch, tmp_path, budget):
    _enable(monkeypatch, tmp_path, budget=budget)
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


def test_agent_assignment_task_type_always_requires_founder_approval():
    for policy in ("standard_repo_work", "autonomous_development_work"):
        assert requires_approval(policy_name=policy, task_type="agent_assignment", task_approval_required=False) is True


# ============================================================================ session turns
# through the real dispatch gate and database

@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _reviewer_assignment(db, owner_id, worktree: str, *, approval_policy="standard_repo_work"):
    goal = create_goal(db, owner_id=owner_id, title="Agent conversation", original_instruction="Talk to an agent.", created_by="founder",
                       approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="conversation test", tasks=[PlannedTaskSpec(description="Ask the agent.", task_type="read_only_audit")],
                       created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db, agent_key=f"claude-code-{uuid.uuid4().hex[:8]}", display_name="Claude Code", adapter_kind="cli",
                           execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    assignment = create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="reviewer", read_write_mode="read_only",
        repository_identity="lifeai", allowed_paths=[], requested_by="test",
    )
    assignment.worktree_path = worktree
    db.flush()
    if assignment.status.value == "planned":
        transition_status(db, assignment=assignment, new_status="ready")
    return task, agent, assignment


@pytest.mark.asyncio
async def test_start_session_records_answer_session_and_audit_then_waits_for_review(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")

    turn = await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="What changed?",
                                     adapter_key=key, require_adapter_enabled=False)

    assert turn.outcome == OUTCOME_ASSIGNABLE and turn.final_message == "first answer" and turn.session_id == SESSION
    superuser_db.refresh(assignment)
    assert assignment.status.value == "waiting_review"
    assert recorded_session_id(assignment, key) == SESSION
    execution = superuser_db.query(AgentDispatchExecution).filter_by(assignment_id=assignment.id).one()
    assert execution.process_ref == SESSION and execution.result_ingestion_status.value == "ingested"
    events = superuser_db.query(AgentWorkAssignmentEvent).filter_by(assignment_id=assignment.id).all()
    audit = [e.detail for e in events if e.detail.get("summary", "").startswith("instruction delivered")]
    assert audit and audit[0]["instruction_excerpt"] == "What changed?" and len(audit[0]["instruction_sha256"]) == 64
    assert len(_calls(log)) == 1


@pytest.mark.asyncio
async def test_continue_session_resumes_the_same_session_and_returns_the_new_answer(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="first", adapter_key=key, require_adapter_enabled=False)

    fresh_adapter = _adapter(tmp_path, "claude")[0]  # a new adapter instance, as after a worker restart
    turn = await continue_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=fresh_adapter, instruction="and the risks?",
                                        adapter_key=key, require_adapter_enabled=False)

    assert turn.outcome == OUTCOME_ASSIGNABLE and turn.final_message == "second answer" and turn.session_id == SESSION
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION
    superuser_db.refresh(assignment)
    assert assignment.status.value == "waiting_review"
    executions = superuser_db.query(AgentDispatchExecution).filter_by(assignment_id=assignment.id).all()
    assert len(executions) == 2 and {e.process_ref for e in executions} == {SESSION}

    close_agent_session(superuser_db, assignment=assignment, accepted=True, reason="answers accepted")
    assert assignment.status.value == "completed"


@pytest.mark.asyncio
async def test_start_refuses_a_second_session_for_the_same_assignment(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="first", adapter_key=key, require_adapter_enabled=False)
    again = await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="again", adapter_key=key, require_adapter_enabled=False)
    assert again.outcome == OUTCOME_SESSION_EXISTS and len(_calls(log)) == 1


@pytest.mark.asyncio
async def test_continue_refuses_without_a_recorded_session_or_outside_review(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    not_waiting = await continue_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="x", adapter_key=key, require_adapter_enabled=False)
    assert not_waiting.outcome == OUTCOME_NOT_AWAITING_INSTRUCTION

    # A session recorded for a DIFFERENT provider is never used -- only this adapter's own.
    await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="first", adapter_key=key, require_adapter_enabled=False)
    other = await continue_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="x", adapter_key="cursor-agent", require_adapter_enabled=False)
    assert other.outcome == OUTCOME_SESSION_UNKNOWN and len(_calls(log)) == 1


@pytest.mark.asyncio
async def test_continue_rechecks_founder_enablement_and_launches_nothing_when_disabled(superuser_db, owner_id, tmp_path, monkeypatch):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="first", adapter_key=key, require_adapter_enabled=False)
    monkeypatch.delenv(f"LIFE_AGENT_ADAPTER_ENABLED__{agent.agent_key.upper().replace('-', '_')}", raising=False)
    turn = await continue_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="x", adapter_key=key, require_adapter_enabled=True)
    assert turn.outcome == OUTCOME_ADAPTER_DISABLED and len(_calls(log)) == 1
    superuser_db.refresh(assignment)
    assert assignment.status.value == "waiting_review"


@pytest.mark.asyncio
async def test_invalid_instruction_is_rejected_before_any_state_change(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    turn = await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="  ", adapter_key=key, require_adapter_enabled=False)
    assert turn.outcome == OUTCOME_INSTRUCTION_REJECTED and assignment.status.value == "ready" and _calls(log) == []


@pytest.mark.asyncio
async def test_failed_turn_transitions_to_failed_with_diagnostics(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, _log, key = _adapter(tmp_path, "claude")
    _behave(tmp_path, exit=2, stderr="model refused")
    turn = await start_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="x", adapter_key=key, require_adapter_enabled=False)
    superuser_db.refresh(assignment)
    assert turn.outcome != OUTCOME_ASSIGNABLE and assignment.status.value == "failed"
    assert any(r.get("kind") == "agent_failure" for r in assignment.output_refs)


@pytest.mark.asyncio
async def test_path_a_handler_launches_nothing_when_the_founder_has_not_enabled_the_agent(superuser_db, owner_id, tmp_path, monkeypatch):
    task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    monkeypatch.delenv(f"LIFE_AGENT_ADAPTER_ENABLED__{agent.agent_key.upper().replace('-', '_')}", raising=False)
    with pytest.raises(AgentSessionError) as excinfo:
        await run_agent_assignment_task(superuser_db, task)
    assert excinfo.value.outcome in ("ADAPTER_DISABLED", "REAL_PROVIDER_NOT_CONFIGURED")


@pytest.mark.asyncio
async def test_path_a_handler_requires_exactly_one_ready_assignment(superuser_db, owner_id, tmp_path):
    task, _agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    transition_status(superuser_db, assignment=assignment, new_status="cancelled")
    with pytest.raises(AgentSessionError) as excinfo:
        await run_agent_assignment_task(superuser_db, task)
    assert excinfo.value.outcome == "NO_UNIQUE_READY_ASSIGNMENT"
