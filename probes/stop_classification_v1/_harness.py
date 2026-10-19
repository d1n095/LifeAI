"""Shared, network-free harness for the STOP / failure-classification regression package.

Real production paths: planner.create_plan -> Worker().run_once() (dispatch, job claim,
run_task_execution_job, worktree creation, local `git commit`, pytest verification, real
`git push`) -> retry / recovery / replan ticks. Stand-ins only:
  - OpenAIProvider.chat is a recorder (every call = one would-be PAID call; nothing leaves the host);
  - "GitHub git" is a local bare repository (file path remote, no credentials, no network);
  - the GitHub REST layer (get_ref / PRs / check runs) is an in-process fake.
Run with a hermetic git environment (see run.sh); never reads host credential helpers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.integrations.github_client import GitHubClient
from app.mainai_execution import ci_wait, planner
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.provider_spend import authorize_provider_spend
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var

LEGACY_BASE = "integration/founder-alpha-test-base"
ACTIVATION_BASE = "mainai/integration-founder-supervised-v1"
PATH_A_CAPS = ["path_a", "read_file", "repo_edit", "run_tests", "open_pr"]
PATHS = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]
OTHER_PATHS = ["backend/app/farewell.py", "backend/tests/test_farewell_doc.py"]
TERMINALISH = {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled,
               MainAITaskStatus.retryable_failed, MainAITaskStatus.blocked}


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _production_spend_gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def seed_remote(tmp_path: Path, base: str) -> Path:
    bare = tmp_path / "github.git"
    git("init", "--bare", "-q", str(bare))
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed))
    (seed / "backend" / "app").mkdir(parents=True)
    (seed / "backend" / "tests").mkdir(parents=True)
    (seed / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    (seed / "backend" / "app" / "farewell.py").write_text("def bye(name):\n    return f'bye {name}'\n")
    git("config", "user.email", "seed@test.local", cwd=seed)
    git("config", "user.name", "Seed", cwd=seed)
    git("checkout", "-q", "-b", base, cwd=seed)
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("push", "-q", "origin", base, cwd=seed)
    return bare


class FakeGitHub:
    def __init__(self, bare: Path, base: str):
        self.bare, self.base, self.prs = bare, base, []
        self.check_runs = [{"name": "Backend — unit/integration tests", "status": "completed", "conclusion": "success"}]
        self.get_ref_override = None  # callable(branch) -> sha | raises


def install_github(monkeypatch, bare: Path, base: str) -> FakeGitHub:
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    for key, value in (("github_write_enabled", True), ("github_repo", "test-owner/test-repo"), ("github_token", "fake-not-used"),
                       ("github_worktree_base_branch", base), ("github_pr_base_branch", base)):
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(bare))
    monkeypatch.setattr(ci_wait, "CI_POLL_BASE_SECONDS", 1, raising=False)
    fake = FakeGitHub(bare, base)

    async def get_ref(self, branch):
        if fake.get_ref_override is not None:
            result = fake.get_ref_override(branch)
            if result is not None:
                return result
        # Mirror the real client: a missing ref is GitHubClientError(404), never a raw subprocess error.
        from app.integrations.github_client import GitHubClientError

        res = subprocess.run(["git", "-C", str(fake.bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise GitHubClientError(f"GitHub GET /repos/test-owner/test-repo/git/ref/heads/{branch} misslyckades (404): "
                                    '{"message":"Not Found"}', status_code=404)
        return res.stdout.strip()

    async def list_prs(self, *, head, base, state="all"):
        return [p for p in fake.prs if p["head"] == head.split(":", 1)[-1] and p["base"] == base]

    async def create_pr(self, *, title, body, head, base, draft=False):
        pr = {"number": len(fake.prs) + 1, "html_url": f"https://example.invalid/pull/{len(fake.prs) + 1}", "head": head, "base": base, "draft": draft}
        fake.prs.append(pr)
        return pr

    async def check_runs(self, ref):
        return list(fake.check_runs)

    async def forbidden_write(self, *args, **kwargs):
        raise AssertionError("unexpected GitHub contents/ref write")

    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "get_ref", get_ref)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", list_prs)
    monkeypatch.setattr(GitHubClient, "create_pull_request", create_pr)
    monkeypatch.setattr(GitHubClient, "list_check_runs", check_runs)
    for name in ("create_or_update_file", "update_ref", "create_branch", "commit_multiple_files"):
        monkeypatch.setattr(GitHubClient, name, forbidden_write)
    return fake


@pytest.fixture
def github(monkeypatch, tmp_path):
    bare = seed_remote(tmp_path, LEGACY_BASE)
    return install_github(monkeypatch, bare, LEGACY_BASE)


# --------------------------------------------------------------------------- paid-call recorder
def edit_payload(paths=PATHS, *, failing_test=False, extra_files=()):
    test_body = "    assert False, 'local verification failure'\n" if failing_test else "    assert m.greet.__doc__\n"
    module = paths[0]
    files = [
        {"path": module, "content": "def greet(name):\n    \"\"\"Return a greeting.\"\"\"\n    return f'hello {name}'\n"
         if "greeting" in module else "def bye(name):\n    \"\"\"Say bye.\"\"\"\n    return f'bye {name}'\n"},
        {"path": paths[1], "content": "import importlib.util, pathlib\n\ndef test_doc():\n"
         f"    p = pathlib.Path(__file__).resolve().parents[1] / 'app' / '{Path(module).name}'\n"
         "    spec = importlib.util.spec_from_file_location('m', p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
         + (test_body if "greeting" in module else ("    assert False\n" if failing_test else "    assert m.bye.__doc__\n"))},
        *extra_files,
    ]
    return json.dumps({"files": files, "commit_message": "Document"})


def plan_payload(*chains):
    """chains: list of target-path lists; each becomes repo_edit (+ open_pr when with_pr)."""
    tasks = []
    for paths, with_pr in chains:
        tasks.append({"description": f"Docstring + test for {paths[0]}.", "task_type": "repo_edit", "depends_on": [], "target_files": list(paths),
                      "verification_plan": [{"kind": "targeted_tests", "target": paths[1].removeprefix("backend/")}]})
        if with_pr:
            tasks.append({"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [len(tasks) - 1]})
    return json.dumps({"tasks": tasks})


class PaidCalls:
    """Every OpenAIProvider.chat invocation is a would-be paid call. kind: planner|code_agent|other."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []  # (kind, target hint)

    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def snapshot(self):
        return len(self.calls)

    def since(self, mark):
        return self.calls[mark:]


def install_provider(monkeypatch, *, plan, edit_for):
    """edit_for(user_prompt) -> JSON edit payload; lets each repo_edit chain get its own edit."""
    paid = PaidCalls()

    async def chat(self, messages, model, **kwargs):
        system = messages[0].content if messages else ""
        user = messages[-1].content if messages else ""
        if "planerare" in system:
            paid.calls.append(("planner", ""))
            content = plan
        elif "kodagent" in system:
            paid.calls.append(("code_agent", "farewell" if "farewell" in user else "greeting"))
            content = edit_for(user)
        else:
            paid.calls.append(("other", ""))
            content = "No issues found."
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 50, "completion_tokens": 50})

    monkeypatch.setattr(OpenAIProvider, "chat", chat)
    return paid


# --------------------------------------------------------------------------- goal + driver
def authorize(db, owner_id, goal_id, *, paths, caps=PATH_A_CAPS):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"p-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                            authorized_paths=list(paths), authorized_capabilities=list(caps), authorized_risk="low",
                                            envelope_idempotency_key=f"e-{uuid.uuid4()}")
    authorize_provider_spend(db, owner_id=owner_id, goal_id=goal_id, execution_envelope_id=envelope.id, authorized_by="founder",
                             max_cost_usd=Decimal("1.00"), max_requests=20, max_cost_per_request_usd=Decimal("0.05"),
                             idempotency_key=f"s-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=[])
    db.commit()
    return envelope


async def planned_goal(db, owner_id, *, paths):
    goal = planner.create_goal(db, owner_id=owner_id, title="Document", original_instruction="Docstring + focused test.", created_by="founder")
    db.commit()
    authorize(db, owner_id, goal.id, paths=paths)
    specs, _, _ = await planner.propose_plan_via_ai(db, goal=goal)
    planner.create_plan(db, goal=goal, rationale="AI plan", tasks=specs, created_by="founder")
    db.commit()
    return goal


def fast_forward(db, goal_id):
    """Make every scheduled retry / wait due NOW (simulates elapsed backoff; no clock mocking)."""
    past = datetime.utcnow() - timedelta(seconds=1)
    db.execute(sa_text("UPDATE mainai_tasks SET next_retry_at=:p WHERE goal_id=:g AND next_retry_at IS NOT NULL"), {"p": past, "g": str(goal_id)})
    db.execute(sa_text("UPDATE mainai_task_waits SET next_poll_at=:p WHERE status='pending' AND task_id IN (SELECT id FROM mainai_tasks WHERE goal_id=:g)"),
               {"p": past, "g": str(goal_id)})
    db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at=:p WHERE status='running' AND id IN (SELECT mainai_job_id FROM mainai_tasks WHERE goal_id=:g)"),
               {"p": past, "g": str(goal_id)})
    db.commit()


async def drive(db, goal_id, *, rounds=4, ticks=12):
    """Run real worker cycles; between rounds, make backoffs/leases due so retries, recovery and
    replans that production WOULD perform later are performed now."""
    from app.worker import Worker

    for _ in range(rounds):
        for _ in range(ticks):
            worked = await Worker().run_once()
            if not worked:
                await asyncio.sleep(0.05)
        fast_forward(db, goal_id)
    db.expire_all()
    return tasks_by_type(db, goal_id)


def tasks_by_type(db, goal_id):
    out = {}
    for t in db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id).order_by(MainAITask.created_at)).scalars().all():
        out.setdefault(t.task_type, []).append(t)
    return out


def jobs_for_task(db, task_id) -> int:
    return db.execute(sa_text(
        "SELECT count(DISTINCT (e.detail->>'mainai_job_id')) FROM mainai_task_events e WHERE e.task_id=:t AND e.event_type='dispatched'"),
        {"t": str(task_id)}).scalar_one()


def task_branches(bare: Path) -> list[str]:
    return [r for r in git("-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if r.startswith("claude/mainai-task-")]


def trail(db, goal_id):
    rows = db.execute(sa_text(
        "SELECT t.task_type, e.event_type, left(coalesce(e.detail::text,''), 160) FROM mainai_task_events e JOIN mainai_tasks t ON t.id=e.task_id "
        "WHERE t.goal_id=:g ORDER BY e.created_at"), {"g": str(goal_id)}).all()
    return [tuple(r) for r in rows][-10:]
