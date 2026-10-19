"""Claude first-cycle E2E-A/B/B2 regressions for FC-1/FC-2/FC-5. No live APIs, no npm-audit
lane (E2E-C), no Track B (E2E-D)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.integrations.github_client import GitHubClient
from app.mainai_execution import ci_wait, planner
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var

BASE = "integration/founder-alpha-test-base"
TERMINALISH = {
    MainAITaskStatus.completed,
    MainAITaskStatus.failed,
    MainAITaskStatus.cancelled,
    MainAITaskStatus.retryable_failed,
}
PATH_A_CAPS = ["path_a", "read_file", "repo_edit", "run_tests", "open_pr"]
PATH_A_PATHS = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]


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
    for key, value in (
        ("github_write_enabled", True),
        ("github_repo", "test-owner/test-repo"),
        ("github_token", "fake-not-used"),
        ("github_worktree_base_branch", BASE),
        ("github_pr_base_branch", BASE),
    ):
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(remote))
    monkeypatch.setattr(ci_wait, "CI_POLL_BASE_SECONDS", 1, raising=False)
    fake = FakeGitHub(remote, [{"name": "Backend — unit/integration tests", "status": "completed", "conclusion": "success"}])

    async def get_ref(self, branch):
        return _git("-C", str(remote), "rev-parse", f"refs/heads/{branch}")

    async def list_prs(self, *, head, base, state="all"):
        return [p for p in fake.prs if p["head"] == head.split(":", 1)[-1] and p["base"] == base]

    async def create_pr(self, *, title, body, head, base, draft=False):
        pr = {
            "number": len(fake.prs) + 1,
            "html_url": f"https://example.invalid/pull/{len(fake.prs) + 1}",
            "head": head,
            "base": base,
            "draft": draft,
        }
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

    async def workflow_runs(self, sha):  # read-only Actions runs for the CI wait; none in this fake
        return []

    monkeypatch.setattr(GitHubClient, "list_workflow_runs_for_sha", workflow_runs)
    for name in ("create_or_update_file", "update_ref", "create_branch", "commit_multiple_files"):
        monkeypatch.setattr(GitHubClient, name, forbidden_write)
    return fake


PLAN = json.dumps(
    {
        "tasks": [
            {
                "description": "Inspect backend/app/greeting.py and confirm greet() lacks a docstring.",
                "task_type": "read_only_audit",
                "depends_on": [],
            },
            {
                "description": "Add a docstring to greet() in backend/app/greeting.py and add a focused test.",
                "task_type": "repo_edit",
                "depends_on": [0],
                "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}],
            },
            {
                "description": "Open a draft PR for the verified change.",
                "task_type": "open_pr",
                "depends_on": [1],
            },
        ]
    }
)
PLAN_WITH_DECLARED_TARGETS = json.dumps(
    {
        "tasks": [
            {
                "description": "Inspect backend/app/greeting.py and confirm greet() lacks a docstring.",
                "task_type": "read_only_audit",
                "depends_on": [],
            },
            {
                "description": "Add a docstring to greet() in backend/app/greeting.py and add a focused test.",
                "task_type": "repo_edit",
                "depends_on": [0],
                "target_files": PATH_A_PATHS,
                "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}],
            },
            {
                "description": "Open a draft PR for the verified change.",
                "task_type": "open_pr",
                "depends_on": [1],
            },
        ]
    }
)
EDIT = json.dumps(
    {
        "files": [
            {
                "path": "backend/app/greeting.py",
                "content": "def greet(name):\n    \"\"\"Return a greeting.\"\"\"\n    return f'hello {name}'\n",
            },
            {
                "path": "backend/tests/test_greeting_doc.py",
                "content": (
                    "import importlib.util, pathlib\n\ndef test_greet_has_docstring():\n"
                    "    p = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'greeting.py'\n"
                    "    spec = importlib.util.spec_from_file_location('greeting', p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                    "    assert m.greet.__doc__\n"
                ),
            },
        ],
        "commit_message": "Document greet()",
    }
)


def _provider(monkeypatch, plan=PLAN, edit=EDIT):
    calls = []

    async def chat(self, messages, model, **kwargs):
        system = messages[0].content if messages else ""
        calls.append("planner" if "planerare" in system else "code_agent" if "kodagent" in system else "other")
        content = plan if "planerare" in system else edit if "kodagent" in system else "No issues found."
        return ChatResult(
            content=content,
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 50, "completion_tokens": 50},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    return calls


async def _drive(db, goal_id, n=30):
    from app.worker import Worker

    idle = 0
    for _ in range(n):
        worked = await Worker().run_once()
        db.expire_all()
        tasks = db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()
        if tasks and all(t.status in TERMINALISH for t in tasks):
            break
        if not worked:
            idle += 1
            if idle > 3:
                break
            await asyncio.sleep(1.2)
            continue
        idle = 0
    db.expire_all()
    return {t.task_type: t for t in db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()}


async def _plan_goal(db, owner_id, *, billable: bool, capabilities=None, paths=None):
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="Document greet()",
        original_instruction="Add a docstring to greet() with a focused test.",
        created_by="founder",
    )
    db.commit()
    if billable:
        proposal = propose_execution_scope(
            db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}"
        )
        _, envelope = authorize_execution_scope(
            db,
            owner_id=owner_id,
            proposal_id=proposal.id,
            authorized_by="founder",
            authorized_paths=list(paths or PATH_A_PATHS),
            authorized_capabilities=list(capabilities or PATH_A_CAPS),
            authorized_risk="low",
            envelope_idempotency_key=f"e-{uuid.uuid4()}",
        )
        authorize_provider_spend(
            db,
            owner_id=owner_id,
            goal_id=goal.id,
            execution_envelope_id=envelope.id,
            authorized_by="founder",
            max_cost_usd=Decimal("1.00"),
            max_requests=10,
            max_cost_per_request_usd=Decimal("0.50"),
            idempotency_key=f"s-{uuid.uuid4()}",
            allowed_providers=["openai"],
            allowed_models=[get_settings().default_llm_model],
        )
        db.commit()
    try:
        specs, _, _ = await planner.propose_plan_via_ai(db, goal=goal)
        planner.create_plan(db, goal=goal, rationale="AI plan", tasks=specs, created_by="founder")
        db.commit()
        return goal, None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return goal, f"{type(exc).__name__}: {str(exc)[:160]}"


@pytest.mark.asyncio
async def test_e2e_a_authorized_billable_goal(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    calls = _provider(monkeypatch, plan=PLAN_WITH_DECLARED_TARGETS)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=True)
    assert plan_error is None, plan_error
    supervisor_ids = {g.id for g, _env in eligible_authorized_goals(superuser_db, limit=50)}
    assert goal.id not in supervisor_ids
    tasks = await _drive(superuser_db, goal.id)
    states = {k: v.status.value for k, v in tasks.items()}
    holds = bool(github.prs) and github.prs[0].get("draft") is True
    M(
        "E2E-A billable authorized goal reaches a draft PR",
        holds,
        f"plan_error={plan_error}; provider calls={calls}; task states={states}; draft PRs={github.prs}",
    )
    assert holds
    assert github.prs[0]["base"] == BASE
    assert len(github.writes) == 0


@pytest.mark.asyncio
async def test_e2e_b_ai_plan_without_target_files_is_accepted(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    calls = _provider(monkeypatch)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=False)
    tasks = await _drive(superuser_db, goal.id) if plan_error is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    events = superuser_db.execute(
        sa_text(
            "SELECT t.task_type, e.event_type, e.detail::text FROM mainai_task_events e "
            "JOIN mainai_tasks t ON t.id=e.task_id WHERE t.goal_id=:g ORDER BY e.created_at"
        ),
        {"g": str(goal.id)},
    ).all()
    refusal = next((d for _, et, d in events if "Refusing" in (d or "")), None)
    pushed_paths = _pushed_paths(github.bare)
    greeting_pushed = any("backend/app/greeting.py" in files for files in pushed_paths.values())
    M(
        "E2E-B AI plan without target_files cannot turn prose-named modules into edit authority",
        not greeting_pushed,
        f"plan_error={plan_error}; states={states}; pushed={pushed_paths}; PRs={github.prs}; refusal={(refusal or '')[:160]}; calls={calls}",
    )
    assert not greeting_pushed
    assert github.prs == []


@pytest.mark.asyncio
async def test_e2e_b2_named_file_cycle(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", False)
    plan = PLAN.replace(
        '"Add a docstring to greet() in backend/app/greeting.py and add a focused test."',
        '"Add a docstring to greet() in backend/app/greeting.py and add a focused test.", '
        '"target_files": ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]',
    )
    calls = _provider(monkeypatch, plan=plan)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=False)
    assert plan_error is None, plan_error
    tasks = await _drive(superuser_db, goal.id)
    states = {k: v.status.value for k, v in tasks.items()}
    pushed = [
        ref
        for ref in _git("-C", str(github.bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n")
        if ref.startswith("claude/mainai-task-")
    ]
    pr = github.prs[0] if github.prs else {}
    holds = bool(pushed) and pr.get("draft") is True and pr.get("base") == BASE
    M(
        "E2E-B2 named-file cycle: verified push + DRAFT PR against the configured base",
        holds,
        f"task states={states}; pushed={pushed}; PR={pr}; provider calls={calls}; contents/ref writes={len(github.writes)}",
    )
    assert holds
    assert states.get("repo_edit") in {"completed", "waiting_ci"} or states.get("open_pr") in {
        "completed",
        "waiting_ci",
    }


def _pushed_paths(bare: Path) -> dict[str, list[str]]:
    refs = [
        ref
        for ref in _git("-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n")
        if ref.startswith("claude/mainai-task-")
    ]
    return {ref: _git("-C", str(bare), "diff", "--name-only", f"{BASE}..{ref}").split("\n") for ref in refs}


@pytest.mark.asyncio
async def test_scope1_sibling_prefix_is_not_pushed(db_session, superuser_db, owner_id, monkeypatch, github):
    from app.mainai_execution.envelope_scope import path_in_authorized_scope

    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    unit = {
        path: path_in_authorized_scope(path, PATH_A_PATHS)
        for path in (
            "backend/app/greeting.py",
            "backend/app/greeting.py_backdoor.py",
            ".backend/app/greeting.py",
            "backend/tests/test_greeting_doc.py.d/x.py",
        )
    }
    plan = json.dumps(
        {
            "tasks": [
                {
                    "description": "Add a helper next to greet().",
                    "task_type": "repo_edit",
                    "depends_on": [],
                    "target_files": ["backend/app/greeting.py_backdoor.py"],
                    "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}],
                },
                {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]},
            ]
        }
    )
    edit = json.dumps(
        {
            "files": [
                {"path": "backend/app/greeting.py_backdoor.py", "content": "print('outside')\n"},
                {"path": "backend/tests/test_greeting_doc.py", "content": "def test_ok():\n    assert True\n"},
            ],
            "commit_message": "helper",
        }
    )
    calls = _provider(monkeypatch, plan=plan, edit=edit)
    goal, plan_error = await _plan_goal(db_session, owner_id, billable=True)
    tasks = await _drive(superuser_db, goal.id) if plan_error is None else {}
    pushed = _pushed_paths(github.bare)
    escaped = any("backend/app/greeting.py_backdoor.py" in files for files in pushed.values())
    states = {k: v.status.value for k, v in tasks.items()}
    M(
        "SCOPE-1 envelope path matching has a path boundary (no sibling-prefix / leading-dot escape)",
        not escaped and not any(v for k, v in unit.items() if k != "backend/app/greeting.py"),
        f"matcher={unit}; pushed={pushed}; PRs={github.prs}; calls={calls}; states={states}",
    )
    assert not escaped
    assert not unit["backend/app/greeting.py_backdoor.py"]
    assert not unit[".backend/app/greeting.py"]
    assert "code_agent" not in calls


@pytest.mark.asyncio
async def test_cap1_repo_edit_does_not_open_a_pr(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    calls = _provider(monkeypatch, plan=PLAN_WITH_DECLARED_TARGETS)
    goal, plan_error = await _plan_goal(
        db_session,
        owner_id,
        billable=True,
        capabilities=["path_a", "read_file", "repo_edit", "run_tests"],
    )
    assert plan_error is None, plan_error
    tasks = await _drive(superuser_db, goal.id)
    states = {k: v.status.value for k, v in tasks.items()}
    holds = (
        not github.prs
        and states.get("repo_edit") in {"completed", "waiting_ci"}
        and states.get("open_pr") in {"failed", "retryable_failed"}
    )
    M(
        "CAP-1 an envelope without open_pr does not open a pull request",
        holds,
        f"PRs={github.prs}; states={states}; calls={calls}",
    )
    assert github.prs == []
    assert states.get("repo_edit") in {"completed", "waiting_ci"}
    assert states.get("open_pr") in {"failed", "retryable_failed"}


@pytest.mark.asyncio
async def test_fc2b_prose_named_file_is_not_writable(db_session, superuser_db, owner_id, monkeypatch, github):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)
    plan = json.dumps(
        {
            "tasks": [
                {
                    "description": "Add a docstring to greet(); also rewrite backend/app/farewell.py.",
                    "task_type": "repo_edit",
                    "depends_on": [],
                    "target_files": PATH_A_PATHS,
                    "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}],
                },
                {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]},
            ]
        }
    )
    files = json.loads(EDIT)["files"] + [{"path": "backend/app/farewell.py", "content": "def bye():\n    return 'bye'\n"}]
    calls = _provider(monkeypatch, plan=plan, edit=json.dumps({"files": files, "commit_message": "x"}))
    goal, plan_error = await _plan_goal(
        db_session,
        owner_id,
        billable=True,
        paths=["backend/app/", "backend/tests/"],
    )
    await _drive(superuser_db, goal.id) if plan_error is None else {}
    pushed = _pushed_paths(github.bare)
    prose_written = any("backend/app/farewell.py" in files for files in pushed.values())
    M(
        "FC-2b only planner-declared target_files are writable (a file named only in prose is refused)",
        not prose_written,
        f"declared={PATH_A_PATHS}; pushed={pushed}; calls={calls}",
    )
    assert not prose_written
