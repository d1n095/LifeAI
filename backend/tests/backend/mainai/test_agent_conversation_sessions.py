"""Agent conversation turns: instruction delivery, answer capture, MainAI-owned session binding,
same-session continuation, isolation from Founder sessions, failures and authorization
boundaries (app.agent_coordination.adapters dialects + app.agent_coordination.session).

No real agent is ever launched: every "CLI" here is a small fake executable written to
tmp_path that emulates the documented output shape of `claude -p --output-format json`,
`cursor-agent -p --output-format json` or `codex exec --json`, and logs the exact argv and
HOME it received so the tests can assert what MainAI actually sends."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from app.agent_coordination import adapter_config
from app.agent_coordination.adapter_config import AdapterConfigurationError, isolated_agent_env, validate_isolated_home
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
    mainai_session_id,
    validate_extra_args,
)
from app.agent_coordination.dispatch import OUTCOME_INSTRUCTION_REJECTED
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, create_work_assignment, register_agent, transition_status
from app.agent_coordination.session import (
    OUTCOME_ADAPTER_DISABLED,
    OUTCOME_NOT_AWAITING_INSTRUCTION,
    OUTCOME_SESSION_EXISTS,
    OUTCOME_TURN_LIMIT,
    OUTCOME_UNSUPPORTED_CAPABILITY,
    AgentSessionError,
    close_agent_session,
    continue_agent_session,
    delivered_turns,
    run_agent_assignment_task,
    start_agent_session,
)
from app.config import get_settings
from app.mainai_execution.approval import requires_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentDispatchExecution, AgentWorkAssignmentEvent
from app.models.mainai_execution import MainAITask

FOREIGN_SESSION = "3f2b8c1e-9d4a-4e7b-8a21-5c6d7e8f9a0b"  # well-formed, but not MainAI's -- e.g. a Founder session


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
    fh.write(json.dumps({{"argv": argv, "home": os.environ.get("HOME"), "claude_dir": os.environ.get("CLAUDE_CONFIG_DIR")}}) + "\n")
behaviour = os.path.join(os.path.dirname(LOG), "behaviour.json")
spec = json.load(open(behaviour)) if os.path.exists(behaviour) else {{}}
if spec.get("sleep"):
    time.sleep(spec["sleep"])
if spec.get("exit"):
    sys.stderr.write(spec.get("stderr", "boom"))
    sys.exit(spec["exit"])
resumed = "--resume" in argv or (len(argv) > 1 and argv[1] == "resume")
answer = spec.get("answer") or ("second answer" if resumed else "first answer")
# A real CLI reports the session it ran in: the id it was given, unless the spec overrides it.
given = None
for flag in ("--session-id", "--resume"):
    if flag in argv:
        given = argv[argv.index(flag) + 1]
session = spec.get("session") or given or "agent-own-session-0001"
if MODE == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": answer}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
else:
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": answer,
                       "session_id": session, "num_turns": 1, "total_cost_usd": 0.01}}))
'''

_KEYS = {"claude": "claude-code", "cursor": "cursor-agent", "codex": "codex"}
_DIALECTS = {"claude": ClaudeCodeDialect, "cursor": CursorAgentDialect, "codex": CodexDialect}


def _fake_cli(tmp_path: Path, mode: str) -> tuple[str, Path]:
    log = tmp_path / f"{mode}-argv.log"
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(_FAKE.format(mode=mode, log=str(log)))
    exe = tmp_path / f"fake-{mode}"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(0o755)
    return str(exe), log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _behave(tmp_path: Path, **spec) -> None:
    (tmp_path / "behaviour.json").write_text(json.dumps(spec))


def _isolated_home(tmp_path: Path) -> Path:
    home = tmp_path / "mainai-agent-home"
    home.mkdir(exist_ok=True)
    return home


def _adapter(tmp_path, mode="claude", *, read_only=True, timeout=30, args=("--model", "sonnet")):
    exe, log = _fake_cli(tmp_path, mode)
    key = _KEYS[mode]
    adapter = LocalCLIAdapter(
        provider_key=key, executable=exe, args_template=args, cwd=str(tmp_path), timeout_seconds=timeout,
        env={"HOME": str(_isolated_home(tmp_path)), "CLAUDE_CONFIG_DIR": str(_isolated_home(tmp_path) / ".claude")},
        dialect=_DIALECTS[mode](), read_only=read_only, max_budget_usd=1.0 if mode == "claude" else None,
    )
    return adapter, log, key


def _expected(owner_id, assignment_id, key="claude-code"):
    return mainai_session_id(owner_id=owner_id, assignment_id=assignment_id, provider_key=key)


# ============================================================================ MainAI-owned session ids

@pytest.mark.asyncio
async def test_claude_session_is_started_with_a_mainai_derived_id_and_the_real_instruction(tmp_path):
    adapter, log, key = _adapter(tmp_path, "claude")
    owner, aid = uuid.uuid4(), uuid.uuid4()
    bound = adapter.bind_mainai_session(aid, owner)
    await adapter.start_assignment(aid, instruction="Summarize backend/app/agent_coordination.")
    result = await adapter.collect_result(aid)

    call = _calls(log)[0]
    argv = call["argv"]
    assert bound == _expected(owner, aid) and uuid.UUID(bound).version == 4
    assert argv[argv.index("--session-id") + 1] == bound
    assert argv[:7] == ["-p", "--output-format", "json", "--permission-mode", "plan", "--max-budget-usd", "1.00"]
    assert argv[-1] == build_prompt(aid, "Summarize backend/app/agent_coordination.") and not argv[-1].startswith("-")
    assert call["home"] == str(_isolated_home(tmp_path))
    assert result.succeeded and result.final_message == "first answer" and result.session_id == bound
    session_ref = next(r for r in result.output_refs if r["kind"] == "agent_session")
    assert session_ref == {"kind": "agent_session", "provider": key, "session_id": bound, "mainai_bound": True}


def test_session_ids_are_distinct_per_assignment_owner_and_provider():
    owner, a1, a2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = {_expected(owner, a1), _expected(owner, a2), _expected(uuid.uuid4(), a1), _expected(owner, a1, "cursor-agent")}
    assert len(ids) == 4 and _expected(owner, a1) == _expected(owner, a1)


def test_session_derivation_refuses_the_default_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "change-me-in-production")
    with pytest.raises(AdapterInstructionError):
        mainai_session_id(owner_id=uuid.uuid4(), assignment_id=uuid.uuid4(), provider_key="claude-code")


@pytest.mark.asyncio
async def test_claude_start_without_a_bound_mainai_session_is_refused(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude")
    with pytest.raises(AdapterInstructionError):
        await adapter.start_assignment(uuid.uuid4(), instruction="x")
    assert _calls(log) == []


@pytest.mark.asyncio
async def test_resume_uses_only_the_bound_id_even_after_a_worker_restart(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude")
    owner, aid = uuid.uuid4(), uuid.uuid4()
    bound = adapter.bind_mainai_session(aid, owner)
    await adapter.start_assignment(aid, instruction="first")
    await adapter.collect_result(aid)

    restarted, _, _ = _adapter(tmp_path, "claude")  # new process: no in-memory state
    with pytest.raises(AdapterSessionUnknownError):
        await restarted.send_instruction(aid, "next")  # nothing bound yet -- never guessed
    restarted.bind_mainai_session(aid, owner)
    await restarted.send_instruction(aid, "next")
    second = await restarted.collect_result(aid)

    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == bound
    assert "--continue" not in argv and "--last" not in argv
    assert second.final_message == "second answer" and second.session_id == bound


@pytest.mark.asyncio
async def test_agent_reporting_a_foreign_session_fails_the_turn_and_is_never_adopted(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude")
    _behave(tmp_path, session=FOREIGN_SESSION)
    owner, aid = uuid.uuid4(), uuid.uuid4()
    bound = adapter.bind_mainai_session(aid, owner)
    await adapter.start_assignment(aid, instruction="x")
    result = await adapter.collect_result(aid)
    assert not result.succeeded and result.session_id is None
    assert "agent_session" not in {r["kind"] for r in result.output_refs}
    failure = next(r for r in result.output_refs if r["kind"] == "agent_failure")
    assert failure["parse_error"].startswith("session_mismatch")
    assert adapter.session_id(aid) == bound  # the binding is unchanged by what the agent claimed


# ============================================================================ isolation from Founder HOME/sessions

def test_isolated_home_must_not_be_the_founder_home_or_a_personal_store(tmp_path):
    founder = tmp_path / "founder"
    (founder / ".claude" / "projects").mkdir(parents=True)
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home(str(founder), founder_home=founder)
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home(str(tmp_path), founder_home=founder)  # an ancestor of the Founder HOME
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home(str(founder / ".claude" / "projects"), founder_home=founder)
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home("relative/home", founder_home=founder)
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home(None, founder_home=founder)
    ok = founder / "mainai-agents" / "claude"
    ok.mkdir(parents=True)
    assert validate_isolated_home(str(ok), founder_home=founder) == ok.resolve()


@pytest.mark.parametrize("forwarded", ["HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"])
def test_env_allowlist_can_never_forward_a_personal_home_variable(monkeypatch, tmp_path, forwarded):
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_HOME__CLAUDE_CODE", str(_isolated_home(tmp_path)))
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__CLAUDE_CODE", f"PATH,{forwarded}")
    with pytest.raises(AdapterConfigurationError):
        isolated_agent_env("claude-code")


def test_isolated_env_roots_every_config_and_session_dir_in_the_isolated_home(monkeypatch, tmp_path):
    home = _isolated_home(tmp_path)
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_HOME__CLAUDE_CODE", str(home))
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__CLAUDE_CODE", "PATH")
    env = isolated_agent_env("claude-code")
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(env[name]) == home.resolve() or home.resolve() in Path(env[name]).parents


def test_conversational_adapter_refuses_the_founder_home_directly(tmp_path):
    exe, _ = _fake_cli(tmp_path, "claude")
    for env in ({"HOME": str(Path.home())}, {}):
        with pytest.raises(AdapterInstructionError):
            LocalCLIAdapter(provider_key="claude-code", executable=exe, args_template=("--model", "x"), cwd=str(tmp_path),
                            timeout_seconds=30, env=env, dialect=ClaudeCodeDialect(), max_budget_usd=1.0)


# ============================================================================ Cursor / Codex: single-shot only

@pytest.mark.parametrize("mode", ["cursor", "codex"])
@pytest.mark.asyncio
async def test_cursor_and_codex_resume_is_disabled_and_their_session_ids_are_never_recorded(tmp_path, mode):
    """FAKE executables only -- no real Cursor or Codex session is launched or consumed."""
    adapter, log, _ = _adapter(tmp_path, mode)
    owner, aid = uuid.uuid4(), uuid.uuid4()
    assert adapter.bind_mainai_session(aid, owner) is None
    assert not adapter.control_capabilities().supports_instruction
    await adapter.start_assignment(aid, instruction="review")
    result = await adapter.collect_result(aid)
    assert result.succeeded and result.final_message == "first answer"
    assert result.session_id is None and "agent_session" not in {r["kind"] for r in result.output_refs}
    with pytest.raises(AdapterInstructionError):
        await adapter.send_instruction(aid, "more")
    argv = _calls(log)[0]["argv"]
    assert "--resume" not in argv and "resume" not in argv and len(_calls(log)) == 1
    if mode == "cursor":
        assert argv[argv.index("--sandbox") + 1] == "enabled" and "--force" not in argv
    else:
        assert 'sandbox_mode="read-only"' in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv


# ============================================================================ bounds and input validation

@pytest.mark.asyncio
async def test_write_mode_uses_accept_edits_never_bypass(tmp_path):
    adapter, log, _ = _adapter(tmp_path, "claude", read_only=False)
    aid = uuid.uuid4()
    adapter.bind_mainai_session(aid, uuid.uuid4())
    await adapter.start_assignment(aid, instruction="edit")
    await adapter.collect_result(aid)
    argv = _calls(log)[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in argv and "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize("bad", ["", "   ", "has\x00nul", "x" * 32_001])
def test_instruction_validation_rejects_bad_input(bad):
    with pytest.raises(AdapterInstructionError):
        build_prompt(uuid.uuid4(), bad)


def test_an_instruction_starting_with_a_dash_can_never_become_an_option():
    assert build_prompt(uuid.uuid4(), "--dangerously-skip-permissions").startswith("MainAI assignment ")


@pytest.mark.parametrize("args", [("--dangerously-skip-permissions",), ("--permission-mode", "bypassPermissions"), ("--force", "x"),
                                  ("--model", "--yolo"), ("--model",), ("--session-id", FOREIGN_SESSION), ("--resume", FOREIGN_SESSION)])
def test_args_template_may_only_select_a_model(args):
    with pytest.raises(AdapterInstructionError):
        validate_extra_args(args)


@pytest.mark.asyncio
async def test_failed_turn_is_not_success_and_keeps_bounded_redacted_diagnostics(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude")
    _behave(tmp_path, exit=3, stderr="token=supersecretvalue " + "e" * 5000)
    aid = uuid.uuid4()
    adapter.bind_mainai_session(aid, uuid.uuid4())
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
    adapter.bind_mainai_session(aid, uuid.uuid4())
    await adapter.start_assignment(aid, instruction="x")
    assert "abcdef123456" not in (await adapter.collect_result(aid)).final_message


@pytest.mark.asyncio
async def test_timeout_kills_the_turn(tmp_path):
    adapter, _log, _ = _adapter(tmp_path, "claude", timeout=1)
    _behave(tmp_path, sleep=5)
    aid = uuid.uuid4()
    adapter.bind_mainai_session(aid, uuid.uuid4())
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


# ============================================================================ factory: Founder enablement and bounds

def _enable(monkeypatch, tmp_path, key="CLAUDE_CODE", args="--model sonnet", budget=None, provider="claude", home=True):
    exe, _log = _fake_cli(tmp_path, provider)
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_ENABLED__{key}", "true")
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_COMMAND__{key}", exe)
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_ARGS__{key}", args)
    if home:
        monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_HOME__{key}", str(_isolated_home(tmp_path)))
    else:
        monkeypatch.delenv(f"LIFE_AGENT_ADAPTER_HOME__{key}", raising=False)
    if budget is not None:
        monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_MAX_BUDGET_USD__{key}", budget)


def test_factory_is_inert_until_the_founder_enables_it(monkeypatch, tmp_path):
    monkeypatch.delenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", raising=False)
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


def test_factory_builds_an_isolated_conversational_adapter_when_enabled(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    adapter = get_real_adapter("claude-code", cwd=str(tmp_path), env={"HOME": str(Path.home())})  # caller env is ignored
    assert isinstance(adapter, LocalCLIAdapter) and adapter.control_capabilities().supports_instruction
    assert adapter._env["HOME"] == str(_isolated_home(tmp_path).resolve())


def test_factory_refuses_without_an_isolated_home(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, home=False)
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


def test_factory_refuses_an_unsafe_args_template(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, args="--dangerously-skip-permissions")
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


@pytest.mark.parametrize("budget", ["0", "-1", "abc", str(adapter_config.HARD_MAX_BUDGET_USD + 1)])
def test_factory_refuses_an_invalid_or_excessive_budget(monkeypatch, tmp_path, budget):
    _enable(monkeypatch, tmp_path, budget=budget)
    assert isinstance(get_real_adapter("claude-code", cwd=str(tmp_path)), NotConfiguredAdapter)


@pytest.mark.parametrize("key,provider", [("CURSOR_AGENT", "cursor"), ("CODEX", "codex")])
def test_unbudgeted_cli_requires_explicit_founder_acknowledgement(monkeypatch, tmp_path, key, provider):
    _enable(monkeypatch, tmp_path, key=key, provider=provider)
    monkeypatch.delenv(f"LIFE_AGENT_ADAPTER_ALLOW_UNBUDGETED__{key}", raising=False)
    assert isinstance(get_real_adapter(_KEYS[provider], cwd=str(tmp_path)), NotConfiguredAdapter)
    monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_ALLOW_UNBUDGETED__{key}", "true")
    adapter = get_real_adapter(_KEYS[provider], cwd=str(tmp_path))
    assert isinstance(adapter, LocalCLIAdapter) and not adapter.control_capabilities().supports_instruction


def test_agent_assignment_task_type_always_requires_founder_approval():
    for policy in ("standard_repo_work", "autonomous_development_work"):
        assert requires_approval(policy_name=policy, task_type="agent_assignment", task_approval_required=False) is True


# ============================================================================ session turns through the real
# dispatch gate and database

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


async def _start(db, assignment, agent, adapter, key, instruction="first"):
    return await start_agent_session(db, assignment=assignment, agent=agent, adapter=adapter, instruction=instruction, adapter_key=key,
                                     require_adapter_enabled=False)


async def _continue(db, assignment, agent, adapter, key, instruction="and the risks?"):
    return await continue_agent_session(db, assignment=assignment, agent=agent, adapter=adapter, instruction=instruction, adapter_key=key,
                                        require_adapter_enabled=False)


@pytest.mark.asyncio
async def test_start_session_binds_mainai_id_records_answer_and_audit_then_waits_for_review(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    turn = await _start(superuser_db, assignment, agent, adapter, key, "What changed?")

    expected = _expected(owner_id, assignment.id)
    assert turn.outcome == OUTCOME_ASSIGNABLE and turn.final_message == "first answer" and turn.session_id == expected
    superuser_db.refresh(assignment)
    assert assignment.status.value == "waiting_review" and delivered_turns(superuser_db, assignment) == 1
    execution = superuser_db.query(AgentDispatchExecution).filter_by(assignment_id=assignment.id).one()
    assert execution.process_ref == expected and execution.result_ingestion_status.value == "ingested"
    events = superuser_db.query(AgentWorkAssignmentEvent).filter_by(assignment_id=assignment.id).all()
    audit = [e.detail for e in events if str(e.detail.get("summary", "")).startswith("instruction delivered")]
    assert audit and audit[0]["instruction_excerpt"] == "What changed?" and len(audit[0]["instruction_sha256"]) == 64
    assert _calls(log)[0]["argv"][_calls(log)[0]["argv"].index("--session-id") + 1] == expected


@pytest.mark.asyncio
async def test_poisoned_output_refs_and_process_ref_never_select_the_resumed_session(superuser_db, owner_id, tmp_path):
    """A forged, well-formed foreign (e.g. Founder) session id is written everywhere a runtime
    writer could reach -- output_refs and every tracking row's process_ref. Resume still targets
    only MainAI's derived id."""
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)
    assignment.output_refs = [*assignment.output_refs,
                              {"kind": "agent_session", "provider": key, "session_id": FOREIGN_SESSION, "mainai_bound": True}]
    for row in superuser_db.query(AgentDispatchExecution).filter_by(assignment_id=assignment.id).all():
        row.process_ref = FOREIGN_SESSION
    superuser_db.flush()

    turn = await _continue(superuser_db, assignment, agent, adapter, key)
    argv = _calls(log)[-1]["argv"]
    expected = _expected(owner_id, assignment.id)
    assert argv[argv.index("--resume") + 1] == expected and FOREIGN_SESSION not in argv
    assert turn.outcome == OUTCOME_ASSIGNABLE and turn.session_id == expected


@pytest.mark.asyncio
async def test_continue_after_a_worker_restart_resumes_the_same_mainai_session(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)

    restarted = _adapter(tmp_path, "claude")[0]  # a fresh adapter: nothing in memory
    turn = await _continue(superuser_db, assignment, agent, restarted, key)
    expected = _expected(owner_id, assignment.id)
    argv = _calls(log)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == expected
    assert turn.outcome == OUTCOME_ASSIGNABLE and turn.final_message == "second answer"
    executions = superuser_db.query(AgentDispatchExecution).filter_by(assignment_id=assignment.id).all()
    assert len(executions) == 2 and {e.process_ref for e in executions} == {expected}
    close_agent_session(superuser_db, assignment=assignment, accepted=True, reason="answers accepted")
    assert assignment.status.value == "completed"


@pytest.mark.asyncio
async def test_a_founder_session_can_never_be_reached(superuser_db, owner_id, tmp_path):
    """The Founder's own session exists only in the Founder's HOME, under an id MainAI never
    derives. The CLI runs with the isolated HOME and is only ever given MainAI's id."""
    founder_home = tmp_path / "founder-home"
    (founder_home / ".claude" / "projects").mkdir(parents=True)
    (founder_home / ".claude" / "projects" / f"{FOREIGN_SESSION}.jsonl").write_text("{}\n")
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)
    await _continue(superuser_db, assignment, agent, adapter, key)
    for call in _calls(log):
        assert FOREIGN_SESSION not in call["argv"]
        assert call["home"] == str(_isolated_home(tmp_path)) and str(founder_home) not in json.dumps(call)
    with pytest.raises(AdapterConfigurationError):
        validate_isolated_home(str(founder_home), founder_home=founder_home)


@pytest.mark.asyncio
async def test_start_refuses_a_second_session_even_if_output_refs_are_wiped(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)
    assignment.output_refs = []  # a runtime writer erases the evidence
    superuser_db.flush()
    again = await _start(superuser_db, assignment, agent, adapter, key, "again")
    assert again.outcome == OUTCOME_SESSION_EXISTS and len(_calls(log)) == 1


@pytest.mark.asyncio
async def test_turn_limit_bounds_the_conversation(superuser_db, owner_id, tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_MAX_TURNS__CLAUDE_CODE", "2")
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)
    assert (await _continue(superuser_db, assignment, agent, adapter, key)).outcome == OUTCOME_ASSIGNABLE
    third = await _continue(superuser_db, assignment, agent, adapter, key, "one more")
    assert third.outcome == OUTCOME_TURN_LIMIT and len(_calls(log)) == 2


@pytest.mark.asyncio
async def test_continue_refuses_outside_review_and_for_single_shot_providers(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    assert (await _continue(superuser_db, assignment, agent, adapter, key)).outcome == OUTCOME_NOT_AWAITING_INSTRUCTION

    _task2, agent2, cursor_assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    cursor, cursor_log, cursor_key = _adapter(tmp_path, "cursor")
    await _start(superuser_db, cursor_assignment, agent2, cursor, cursor_key)
    refused = await _continue(superuser_db, cursor_assignment, agent2, cursor, cursor_key)
    assert refused.outcome == OUTCOME_UNSUPPORTED_CAPABILITY and len(_calls(cursor_log)) == 1


@pytest.mark.asyncio
async def test_continue_rechecks_founder_enablement_and_launches_nothing_when_disabled(superuser_db, owner_id, tmp_path, monkeypatch):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    await _start(superuser_db, assignment, agent, adapter, key)
    turn = await continue_agent_session(superuser_db, assignment=assignment, agent=agent, adapter=adapter, instruction="x", adapter_key=key,
                                        require_adapter_enabled=True)
    assert turn.outcome == OUTCOME_ADAPTER_DISABLED and len(_calls(log)) == 1
    superuser_db.refresh(assignment)
    assert assignment.status.value == "waiting_review"


@pytest.mark.asyncio
async def test_invalid_instruction_is_rejected_before_any_state_change(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, log, key = _adapter(tmp_path, "claude")
    turn = await _start(superuser_db, assignment, agent, adapter, key, "  ")
    assert turn.outcome == OUTCOME_INSTRUCTION_REJECTED and assignment.status.value == "ready" and _calls(log) == []


@pytest.mark.asyncio
async def test_failed_turn_transitions_to_failed_with_diagnostics(superuser_db, owner_id, tmp_path):
    _task, agent, assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
    adapter, _log, key = _adapter(tmp_path, "claude")
    _behave(tmp_path, exit=2, stderr="model refused")
    turn = await _start(superuser_db, assignment, agent, adapter, key, "x")
    superuser_db.refresh(assignment)
    assert turn.outcome != OUTCOME_ASSIGNABLE and assignment.status.value == "failed"
    assert any(r.get("kind") == "agent_failure" for r in assignment.output_refs)


@pytest.mark.asyncio
async def test_path_a_handler_launches_nothing_when_the_founder_has_not_enabled_the_agent(superuser_db, owner_id, tmp_path, monkeypatch):
    task, agent, _assignment = _reviewer_assignment(superuser_db, owner_id, str(tmp_path))
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
