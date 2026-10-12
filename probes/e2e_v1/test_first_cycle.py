"""Examiner-only END-TO-END trace of MainAI's first supervised self-development cycle on the local
composition of #263 + #264/#266 + #265/#267 over 2fbe20a. Everything runs through the REAL production
call paths (create_goal -> propose_plan_via_ai -> create_plan -> Worker().run_once() -> execution job
-> worktree -> pytest -> push -> draft PR -> CI wait -> agent_assignment review), with only these
stand-ins: a mocked chat provider, a local bare git remote instead of GitHub git, a fake GitHub REST
layer, and a fake `claude` executable. No paid API, no network, no real agent.

Each scenario prints `MATRIX|id|HOLDS/GAP|evidence` and never raises on GAP."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.integrations.github_client import GitHubClient
from app.mainai_execution import ci_wait, planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var

BASE = "integration/founder-alpha-test-base"
TERMINALISH = {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled, MainAITaskStatus.retryable_failed}


def M(pid, holds, ev):
    print(f"\nMATRIX|{pid}|{'HOLDS' if holds else 'GAP'}|{ev}")


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def remote(tmp_path) -> Path:
    """A local bare repo standing in for GitHub git, seeded on the configured integration base with
    one existing file (the docstring target) and a backend/ layout."""
    bare = tmp_path / "github.git"
    _git("init", "--bare", "-q", str(bare))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed))
    (seed / "backend" / "app").mkdir(parents=True)
    (seed / "backend" / "tests").mkdir(parents=True)
    (seed / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    _git("config", "user.email", "seed@test.local", cwd=seed)
    _git("config", "user.name", "Seed", cwd=seed)
    _git("checkout", "-q", "-b", BASE, cwd=seed)
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    _git("push", "-q", "origin", BASE, cwd=seed)
    return bare


class FakeGitHub:
    def __init__(self, bare: Path, check_runs: list[dict]):
        self.bare, self.check_runs, self.prs, self.writes = bare, check_runs, [], []


@pytest.fixture
def github(monkeypatch, remote):
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    for key, value in (("github_write_enabled", True), ("github_repo", "test-owner/test-repo"), ("github_token", "fake-not-used"),
                       ("github_worktree_base_branch", BASE), ("github_pr_base_branch", BASE)):
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(remote))
    monkeypatch.setattr(ci_wait, "CI_POLL_BASE_SECONDS", 1, raising=False)
    fake = FakeGitHub(remote, [{"name": "Backend — unit/integration tests", "status": "completed", "conclusion": "success"}])

    async def get_ref(self, branch):
        return _git("-C", str(remote), "rev-parse", f"refs/heads/{branch}")

    async def list_prs(self, *, head, base, state="all"):
        return [p for p in fake.prs if p["head"] == head.split(":", 1)[-1] and p["base"] == base]

    async def create_pr(self, *, title, body, head, base, draft=False):
        pr = {"number": len(fake.prs) + 1, "html_url": f"https://example.invalid/pull/{len(fake.prs) + 1}", "head": head, "base": base, "draft": draft}
        fake.prs.append(pr)
        return pr

    async def check_runs(self, ref):
        return list(fake.check_runs)

    async def forbidden_write(self, *args, **kwargs):
        fake.writes.append(args)
        raise AssertionError("unexpected GitHub contents/ref write")

    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "get_ref", get_ref)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", list_prs)
    monkeypatch.setattr(GitHubClient, "create_pull_request", create_pr)
    monkeypatch.setattr(GitHubClient, "list_check_runs", check_runs)
    for name in ("create_or_update_file", "update_ref", "create_branch", "commit_multiple_files"):
        monkeypatch.setattr(GitHubClient, name, forbidden_write)
    return fake


# A realistic AI plan for "add a docstring + a focused test". The planner prompt asks for task
# descriptions and verification plans; it does NOT ask for target files, so -- as a real model would --
# the edit description names the module but not the new test file.
PLAN = json.dumps({"tasks": [
    {"description": "Inspect backend/app/greeting.py and confirm greet() lacks a docstring.", "task_type": "read_only_audit", "depends_on": []},
    {"description": "Add a docstring to greet() in backend/app/greeting.py and add a focused test.", "task_type": "repo_edit", "depends_on": [0],
     "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
    {"description": "Open a draft PR for the verified change.", "task_type": "open_pr", "depends_on": [1]},
]})
EDIT = json.dumps({"files": [
    {"path": "backend/app/greeting.py", "content": "def greet(name):\n    \"\"\"Return a greeting.\"\"\"\n    return f'hello {name}'\n"},
    {"path": "backend/tests/test_greeting_doc.py",
     "content": "import importlib.util, pathlib\n\ndef test_greet_has_docstring():\n"
                "    p = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'greeting.py'\n"
                "    spec = importlib.util.spec_from_file_location('greeting', p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                "    assert m.greet.__doc__\n"},
], "commit_message": "Document greet()"})


def _provider(monkeypatch, plan=PLAN, edit=EDIT):
    calls = []

    async def chat(self, messages, model, **kwargs):
        system = messages[0].content if messages else ""
        calls.append("planner" if "planerare" in system else "code_agent" if "kodagent" in system else "other")
        content = plan if "planerare" in system else edit if "kodagent" in system else "No issues found."
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 50, "completion_tokens": 50})

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    return calls


def _trail(db, goal_id):
    rows = db.execute(sa_text(
        "SELECT t.task_type, e.event_type, left(coalesce(e.detail::text,''), 140) FROM mainai_task_events e JOIN mainai_tasks t ON t.id=e.task_id "
        "WHERE t.goal_id=:g ORDER BY e.created_at"), {"g": str(goal_id)}).all()
    jobs = db.execute(sa_text("SELECT j.job_type, j.status, coalesce(j.error_category::text,'') FROM mainai_jobs j JOIN mainai_tasks t ON t.mainai_job_id=j.id WHERE t.goal_id=:g"),
                      {"g": str(goal_id)}).all()
    waits = db.execute(sa_text("SELECT w.status, w.next_poll_at, now() FROM mainai_task_waits w JOIN mainai_tasks t ON t.id=w.task_id WHERE t.goal_id=:g"), {"g": str(goal_id)}).all()
    return {"events": [tuple(r) for r in rows][-8:], "jobs": [tuple(r) for r in jobs], "waits": [tuple(map(str, r)) for r in waits]}


async def _drive(db, goal_id, n=30):
    from app.worker import Worker

    for _ in range(n):
        worked = await Worker().run_once()
        db.expire_all()
        tasks = db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()
        if tasks and all(t.status in TERMINALISH for t in tasks):
            break
        if not worked:
            idle = getattr(_drive, "_idle", 0) + 1
            _drive._idle = idle
            if idle > 3:
                break
            await asyncio.sleep(1.2)  # let a due CI-wait poll (1 s backoff) become due
            continue
        _drive._idle = 0
    _drive._idle = 0
    db.expire_all()
    return {t.task_type: t for t in db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()}


async def _plan_goal(db, owner_id, *, billable: bool):
    goal = planner.create_goal(db, owner_id=owner_id, title="Document greet()", original_instruction="Add a docstring to greet() with a focused test.",
                               created_by="founder")
    db.commit()
    if billable:
        proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
        _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                                authorized_paths=["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"],
                                                authorized_capabilities=["read_file", "patch_file", "create_file"], authorized_risk="low",
                                                envelope_idempotency_key=f"e-{uuid.uuid4()}")
        authorize_provider_spend(db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
                                 max_cost_usd=Decimal("1.00"), max_requests=10, max_cost_per_request_usd=Decimal("0.50"),
                                 idempotency_key=f"s-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=[get_settings().openai_chat_model]
                                 if hasattr(get_settings(), "openai_chat_model") else [])
        db.commit()
    try:
        specs, _, _ = await planner.propose_plan_via_ai(db, goal=goal)
        planner.create_plan(db, goal=goal, rationale="AI plan", tasks=specs, created_by="founder")
        db.commit()
        return goal, None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return goal, f"{type(exc).__name__}: {str(exc)[:120]}"


# ============================================================================ E2E-A: the authorized, billable configuration
@pytest.mark.asyncio
async def test_e2e_a_authorized_billable_goal(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)  # production default
    calls = _provider(monkeypatch)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=True)
    tasks = await _drive(superuser_db, goal.id) if plan_error is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    M("E2E-A billable authorized goal reaches a draft PR", bool(github.prs),
      f"plan_error={plan_error}; provider calls={calls}; task states={states}; draft PRs={github.prs}; trail={_trail(superuser_db, goal.id)}")


# ============================================================================ E2E-B: write path with routing/spend gates relaxed (find downstream failures)
@pytest.mark.asyncio
async def test_e2e_b_write_path_downstream(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    calls = _provider(monkeypatch)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=False)
    tasks = await _drive(superuser_db, goal.id) if plan_error is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    pushed = [ref for ref in _git("-C", str(github.bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if ref.startswith("claude/mainai-task-")]
    events = superuser_db.execute(sa_text(
        "SELECT t.task_type, e.event_type, e.detail::text FROM mainai_task_events e JOIN mainai_tasks t ON t.id=e.task_id WHERE t.goal_id=:g ORDER BY e.created_at"),
        {"g": str(goal.id)}).all()
    refusal = next((d for _, et, d in events if "Refusing" in (d or "")), None)
    M("E2E-B AI-planned edit (module + new focused test) is accepted by the CTX gate", refusal is None,
      f"refusal={(refusal or '')[:160]}")
    M("E2E-B full cycle reaches a verified push and a draft PR", bool(github.prs) and bool(pushed),
      f"task states={states}; pushed={pushed}; PRs={github.prs}; provider calls={calls}")


@pytest.mark.asyncio
async def test_e2e_b2_write_path_when_the_planner_names_every_file(db_session, superuser_db, owner_id, monkeypatch, github):
    """Best case: the plan names both files, so the CTX gate can accept the edit."""
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    plan = PLAN.replace("and add a focused test.", "and add a focused test in backend/tests/test_greeting_doc.py.")
    calls = _provider(monkeypatch, plan=plan)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=False)
    tasks = await _drive(superuser_db, goal.id) if plan_error is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    pushed = [ref for ref in _git("-C", str(github.bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if ref.startswith("claude/mainai-task-")]
    pr = github.prs[0] if github.prs else {}
    M("E2E-B2 named-file cycle: verified push + DRAFT PR against the configured base, open_pr completed",
      bool(pushed) and pr.get("draft") is True and pr.get("base") == BASE and states.get("open_pr") == "completed",
      f"task states={states}; pushed={pushed}; PR={pr}; provider calls={calls}; contents/ref writes={len(github.writes)}; trail={_trail(superuser_db, goal.id)}")


@pytest.mark.asyncio
async def test_e2e_c_realistic_ci_with_the_inherited_npm_audit_failure(db_session, superuser_db, owner_id, monkeypatch, github):
    """GitHub today reports `Frontend — npm audit` FAILED (and the aggregate 'All required checks passed' FAILED)
    on every branch. A MainAI draft PR would carry the same checks."""
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    github.check_runs[:] = [
        {"name": "Backend — unit/integration tests", "status": "completed", "conclusion": "success"},
        {"name": "Frontend — npm audit", "status": "completed", "conclusion": "failure"},
        {"name": "All required checks passed", "status": "completed", "conclusion": "failure"},
    ]
    plan = PLAN.replace("and add a focused test.", "and add a focused test in backend/tests/test_greeting_doc.py.")
    calls = _provider(monkeypatch, plan=plan)
    goal, _ = await _plan_goal(db_session, owner_id, billable=False)
    tasks = await _drive(superuser_db, goal.id, n=40)
    states = {k: v.status.value for k, v in tasks.items()}
    replans = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_plans WHERE goal_id=:g"), {"g": str(goal.id)}).scalar_one()
    M("E2E-C inherited npm-audit CI failure does not fail the MainAI change or trigger paid replans",
      states.get("open_pr") == "completed" and replans == 1,
      f"task states={states}; plans for goal={replans}; provider calls={calls}")


# ============================================================================ E2E-D: independent review through Track B
@pytest.mark.asyncio
async def test_e2e_d_independent_review_via_track_b(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    from app.agent_coordination.bootstrap import bootstrap_known_agents
    from app.agent_coordination.service import create_work_assignment, transition_status
    from app.models.agent_coordination import AgentWorkAssignment

    home = tmp_path / "mainai-agent-home"
    home.mkdir()
    log = tmp_path / "claude.log"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, os, sys\nargv = sys.argv[1:]\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': argv, 'home': os.environ.get('HOME')}}) + '\\n')\n"
        "sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else argv[argv.index('--resume') + 1]\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'VERDICT: FREEZE (fake)', 'session_id': sid}))\n")
    exe = tmp_path / "claude"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(0o755)
    for k, v in (("ENABLED", "true"), ("COMMAND", str(exe)), ("ARGS", "--model sonnet"), ("HOME", str(home)), ("MAX_BUDGET_USD", "0.50"), ("ENV_ALLOWLIST", "PATH")):
        monkeypatch.setenv(f"LIFE_AGENT_ADAPTER_{k}__CLAUDE_CODE", v)

    goal = planner.create_goal(db_session, owner_id=owner_id, title="Independent review", original_instruction="Review the draft PR.", created_by="founder")
    planner.create_plan(db_session, goal=goal, rationale="review", created_by="founder",
                        tasks=[PlannedTaskSpec(description="Review the MainAI draft PR at its exact head SHA and return FREEZE or RETURN_TO_BUILDER.",
                                               task_type="agent_assignment", verification_plan=[])])
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    # No HTTP route exists to register agents or create coordination assignments: done via services here.
    agent = next(a for a in bootstrap_known_agents(db_session) if a.agent_key == "claude-code")
    review_tree = tmp_path / "review-checkout"
    review_tree.mkdir()
    assignment = create_work_assignment(db_session, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="reviewer",
                                        read_write_mode="read_only", repository_identity="test-owner/test-repo", allowed_paths=[], requested_by="founder")
    assignment.worktree_path = str(review_tree)
    if assignment.status.value == "planned":
        transition_status(db_session, assignment=assignment, new_status="ready")
    db_session.commit()
    grant_task_approval(db_session, task=task, approved_by="founder")
    db_session.commit()

    tasks = await _drive(superuser_db, goal.id)
    state = tasks["agent_assignment"].status.value if "agent_assignment" in tasks else None
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    superuser_db.expire_all()
    a = superuser_db.get(AgentWorkAssignment, assignment.id)
    answer = next((r.get("text") for r in (a.output_refs or []) if r.get("kind") == "agent_final_message"), None)
    registry = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_verification_records")).scalar_one()
    M("E2E-D Track B review runs through the worker with the isolated HOME and returns the reviewer's answer",
      state == "completed" and bool(calls) and calls[0]["home"] == str(home) and answer is not None,
      f"task={state}; cli calls={len(calls)}; cli HOME isolated={bool(calls) and calls[0]['home'] == str(home)}; answer={answer!r}; assignment={a.status.value}")
    M("E2E-D the reviewer's verdict becomes a verification receipt bound to the PR head SHA", registry > 0,
      f"mainai_verification_records rows={registry}; the answer stays a self-report on the assignment only")
