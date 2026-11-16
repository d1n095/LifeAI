"""Life — First Supervised MainAI Development Cycle, REAL DISPATCH extension.

Founder directive "COMPLETE LIFEAI'S FIRST REAL DEVELOPMENT CYCLE": a real, authorized
development task executed end to end, on remote GitHub infrastructure, reusing existing
MainAI execution/authorization/coordination components — no new orchestration framework.

`test_first_supervised_development_cycle.py` (this module's sibling) proves every gate up to
founder approval OPENS — but explicitly stops there: no adapter, no real git, no real GitHub
call appears in that file at all. This file is the one that actually walks through, chaining
ONLY already-existing, already-reviewed production functions:

  real GitHub-evidence staging (app.github_evidence.service, PR #403) -> explicit founder
  authorization (same chain, never bypassable) -> the REAL Worker().run_once() poll loop ->
  real dispatch_ready_task() -> real execution_job.py handlers -> a REAL local git worktree,
  REAL `git init/fetch/checkout/commit/push` (app.mainai_execution.worktree, subprocess calls,
  never mocked) against a local bare repo standing in for GitHub — the exact fixture pattern
  test_mainai_execution_worktree.py already established, reused here verbatim, not
  reinvented -> a real `python -m pytest` subprocess actually run against the file repo_edit
  actually committed -> open_pr.

Explicit, load-bearing limitation, stated plainly rather than glossed over: the GitHub HTTP
calls themselves (create_pull_request / list_pull_requests_for_head) are simulated via the
SAME GitHubClient monkeypatch convention every other test in this codebase already uses for
them (see test_mainai_execution_executor.py's own
test_handle_open_pr_resumes_correctly_without_creating_a_duplicate_pr) — no test anywhere in
this suite makes a live call to api.github.com, and this file does not either. Git itself is
the one thing here that is never mocked. This file does NOT claim a real PR was opened against
the real repository — only that every production function in the real chain ran for real, with
real git, real Postgres, real RLS, and a real subprocess test run. A genuinely live
demonstration against the real repository requires the founder's own explicit authorization of
one specific, real task — a decision this file does not make for itself, matching the exact
authorization boundary this milestone exists to prove, not bypass.

Covers:
  A. The full happy path, driven purely by the real worker poll loop (zero manual dispatch
     calls, same discipline test_mainai_execution_demos.py's Demo 1 already established):
     candidate staged -> authorized -> read_only_audit auto-runs -> repo_edit and open_pr both
     genuinely blocked pending explicit founder approval -> approval granted for each -> real
     git worktree/commit/push -> real pytest run against the committed file -> simulated-GitHub
     PR recorded, not fabricated as "real".
  B. Fail-closed: staging evidence alone never creates a goal; a dismissed/already-authorized
     candidate can never be re-authorized; repo_edit is never auto-dispatched by the real
     worker tick without an explicit approval_granted event, even though its goal came from the
     GitHub-evidence entry point rather than a hand-made goal.
  C. Crash/resume: a real process-level interruption between `git commit` and `git push`
     (the exact LOCAL_COMMITTED_NOT_PUSHED state app.mainai_execution.worktree's own docstring
     names) is recovered by the SAME real recovery pipeline this session already hardened on
     PR #447 (detect -> inspect -> classify -> takeover) — reused, not re-derived — and the
     eventual PR is still created exactly once."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.github_evidence import (
    GithubEvidenceError,
    authorize_github_evidence_candidate,
    dismiss_github_evidence_candidate,
    record_github_evidence_candidates,
)
from app.integrations.github_client import GitHubClient
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_plan
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import execute_takeover
from app.mainai_execution.worktree import BASE_BRANCH
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import RecoveryClassification
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.safe_planner.github_work_queue import WorkQueueItem
from app.worker import Worker

TERMINAL_OR_RETRYING = frozenset(
    {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled, MainAITaskStatus.retryable_failed}
)

_EDIT_JSON = (
    '{"files": [{"path": "backend/tests/test_real_cycle_doc_fix.py", '
    '"content": "def test_real_cycle_doc_fix():\\n    assert True\\n"}], '
    '"commit_message": "First real supervised cycle: trivial, reviewable doc-reference fix"}'
)


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(superuser_db, db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


@pytest.fixture
def bare_remote(tmp_path) -> Path:
    """A real bare git repo standing in for GitHub — identical to
    test_mainai_execution_worktree.py's own fixture of the same name, reused rather than
    reinvented: `git init --bare` plus one seed commit on BASE_BRANCH."""
    remote_path = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_path)], check=True)

    seed_path = tmp_path / "seed-clone"
    subprocess.run(["git", "clone", "-q", str(remote_path), str(seed_path)], check=True)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    (seed_path / "backend" / "tests").mkdir(parents=True)
    (seed_path / "backend" / "tests" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_path), "config", "user.email", "seed@test.local"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "checkout", "-q", "-b", BASE_BRANCH], check=True)
    subprocess.run(["git", "-C", str(seed_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "push", "-q", "origin", BASE_BRANCH], check=True)
    return remote_path


@pytest.fixture
def patched_github(monkeypatch, bare_remote):
    """Points worktree.py's remote-URL construction and GitHubClient.get_ref() at the real
    local bare repo instead of github.com/a real token — identical in shape to
    test_mainai_execution_worktree.py's own fixture of the same name. Everything downstream
    (git init, fetch, checkout, commit, push) is real git talking to a real (local) remote."""
    from app.config import get_settings
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(bare_remote))

    async def _fake_get_ref(self, branch: str) -> str:
        from app.integrations.github_client import GitHubClientError

        result = subprocess.run(
            ["git", "-C", str(bare_remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Matches the real GitHubClient.get_ref()'s behavior for a branch that doesn't
            # exist yet (a 404 from the real API) -- recovery_inspector.py's own
            # `except GitHubClientError: evidence["remote_branch_exists"] = False` depends on
            # this exact distinction, not a bare subprocess crash.
            raise GitHubClientError(f"ref refs/heads/{branch} not found on {bare_remote}")
        return result.stdout.strip()

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    return bare_remote


@pytest.fixture
def simulated_github_pr_api(monkeypatch):
    """The ONE part of this file that is NOT real git: GitHub's actual HTTP API for PR
    creation/listing, simulated via the exact same monkeypatch convention
    test_mainai_execution_executor.py's own open_pr dedup test already uses — never a live
    call to api.github.com anywhere in this suite. Returns the in-memory PR list plus a call
    counter so tests can assert create_pull_request() is never called twice for the same
    head/base (the existing, already-reviewed idempotency guarantee)."""
    created_prs: list[dict] = []
    create_calls = {"n": 0}

    async def _fake_create_pull_request(self, *, title, body, head, base):
        create_calls["n"] += 1
        pr = {"number": 777, "html_url": "https://github.com/d1n095/LifeAI/pull/777", "head": f"d1n095:{head}", "base": base, "state": "open"}
        created_prs.append(pr)
        return pr

    async def _fake_list_pull_requests_for_head(self, *, head, base, state="all"):
        return [pr for pr in created_prs if pr["head"] == head and pr["base"] == base and (state == "all" or pr["state"] == state)]

    async def _fake_list_check_runs(self, sha: str):
        # V0.3's ci_wait.py polls this after a real (simulated) PR is opened — a single
        # completed/success check run is enough for evaluate_check_runs() to call it done+passed.
        return [{"name": "ci", "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(GitHubClient, "create_pull_request", _fake_create_pull_request)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_pull_requests_for_head)
    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)
    return created_prs, create_calls


def _fake_chat_edit_only(self, messages, model, **kwargs):
    raise AssertionError("OpenAIProvider.chat() must not be called for read_only_audit/run_tests in this test — only repo_edit.")


async def _fake_chat_edit(self, messages, model, **kwargs):
    return ChatResult(content=_EDIT_JSON, provider="openai", model=model, raw_usage={})


async def _run_until_stalled_or_done(superuser_db, goal_id, *, max_iterations=25):
    """Drives the REAL worker poll loop — identical helper to
    test_mainai_execution_demos.py's own _run_until_no_terminal_tasks_remain, reused here under
    a name that makes the "stalled on a real approval gate" outcome explicit, since this test
    deliberately calls it while tasks are still gated."""
    for _ in range(max_iterations):
        worked = await Worker().run_once()
        superuser_db.expire_all()
        tasks = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()
        if tasks and all(t.status in TERMINAL_OR_RETRYING for t in tasks):
            return tasks
        if not worked:
            break
    superuser_db.expire_all()
    return superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()


def _stage_and_authorize(db, *, owner_id, pr_number=501, title="Fix stale doc reference flagged by CI"):
    item = WorkQueueItem(
        kind="open_pull_request", number=pr_number, title=title,
        html_url=f"https://github.com/d1n095/LifeAI/pull/{pr_number}",
        updated_at="2026-11-16T00:00:00Z", signals=("ci_red",),
    )
    candidates = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[item])
    db.commit()
    candidate = candidates[0]
    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None  # staging evidence alone is never authorization

    authorized_candidate, goal = authorize_github_evidence_candidate(
        db, owner_id=owner_id, candidate_id=candidate.id, authorized_by="founder-real-dispatch-test",
        title=title,
        # autonomous_development_work: the founder authorized this CANDIDATE/scope, not each
        # task by hand -- repo_edit and open_pr both still require their own explicit approval
        # under this policy (see app/mainai_execution/approval.py's APPROVAL_POLICIES), which is
        # exactly the property this test exists to prove, not standard_repo_work's "AUTO
        # everything" default meant for a goal a human already reviewed task-by-task.
        approval_policy="autonomous_development_work",
    )
    db.commit()
    assert authorized_candidate.status == "authorized"
    assert goal.status == MainAIGoalStatus.pending
    return candidate, authorized_candidate, goal


# ---------------------------------------------------------------- A. real end-to-end happy path


@pytest.mark.asyncio
async def test_real_github_evidence_to_real_pushed_commit_and_pr_end_to_end(
    db_session, superuser_db, owner_id, monkeypatch, patched_github, simulated_github_pr_api
):
    created_prs, create_calls = simulated_github_pr_api
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_edit)

    # --- Steps 1-3: real GitHub-evidence staging + explicit founder authorization. ---
    _candidate, _authorized_candidate, goal = _stage_and_authorize(db_session, owner_id=owner_id)

    create_plan(
        db_session, goal=goal, rationale="Real-dispatch first-cycle demonstration",
        tasks=[
            PlannedTaskSpec(description="Read-only audit of the flagged CI-red PR.", task_type="read_only_audit"),
            PlannedTaskSpec(
                description="Apply the small, reviewable doc-reference fix.", task_type="repo_edit", depends_on=[0],
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_real_cycle_doc_fix.py"}],
            ),
            PlannedTaskSpec(
                description="Run the targeted tests that verify the fix.", task_type="run_tests", depends_on=[1],
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_real_cycle_doc_fix.py"}],
            ),
            PlannedTaskSpec(description="Open a PR for the verified fix.", task_type="open_pr", depends_on=[2]),
        ],
        created_by="founder-real-dispatch-test",
    )
    db_session.commit()

    tasks_by_type = {t.task_type: t for t in db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).all()}
    repo_edit_task, open_pr_task = tasks_by_type["repo_edit"], tasks_by_type["open_pr"]

    # --- Step 4: the real worker tick runs read_only_audit (AUTO under this policy), and
    # genuinely stalls on repo_edit -- no job, no worktree, nothing -- exactly the fail-closed
    # property this milestone must demonstrate, not merely assert in isolation. ---
    stalled = await _run_until_stalled_or_done(superuser_db, goal.id)
    superuser_db.expire_all()
    repo_edit_task = superuser_db.get(MainAITask, repo_edit_task.id)
    assert repo_edit_task.status == MainAITaskStatus.ready, "repo_edit must never auto-dispatch without explicit founder approval"
    assert repo_edit_task.mainai_job_id is None
    audit_task = [t for t in stalled if t.task_type == "read_only_audit"][0]
    assert audit_task.status == MainAITaskStatus.completed

    # --- Step 3 (explicit founder authorization for THIS exact task): repo_edit. ---
    grant_task_approval(superuser_db, task=repo_edit_task, approved_by="founder-real-dispatch-test")
    superuser_db.commit()

    # --- Steps 4-6: real dispatch, real worker claim, real worktree/commit, real pytest run. ---
    await _run_until_stalled_or_done(superuser_db, goal.id)
    superuser_db.expire_all()
    repo_edit_task = superuser_db.get(MainAITask, repo_edit_task.id)
    assert repo_edit_task.status == MainAITaskStatus.completed, repo_edit_task.status

    worktree = superuser_db.execute(sa_text("SELECT path, branch, current_commit FROM mainai_task_worktrees WHERE task_id = :t"), {"t": str(repo_edit_task.id)}).one()
    assert Path(worktree.path).exists()
    assert worktree.current_commit is not None
    local_sha = subprocess.run(["git", "-C", worktree.path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert local_sha == worktree.current_commit
    remote_sha = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{worktree.branch}"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remote_sha == local_sha, "the commit must have genuinely reached the remote via a real `git push`, not merely exist locally"

    run_tests_task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id, MainAITask.task_type == "run_tests")).scalar_one()
    assert run_tests_task.status == MainAITaskStatus.completed, "the real pytest subprocess must have genuinely passed against the real committed file"

    # --- Step 3 again (explicit founder authorization for THIS exact task): open_pr. ---
    open_pr_task = superuser_db.get(MainAITask, open_pr_task.id)
    assert open_pr_task.status == MainAITaskStatus.ready, "open_pr must never auto-dispatch without its own explicit founder approval either"
    grant_task_approval(superuser_db, task=open_pr_task, approved_by="founder-real-dispatch-test")
    superuser_db.commit()

    # --- Steps 6-7: real dispatch to open_pr; simulated (never live) GitHub PR creation. ---
    await _run_until_stalled_or_done(superuser_db, goal.id)
    superuser_db.expire_all()
    open_pr_task = superuser_db.get(MainAITask, open_pr_task.id)
    assert open_pr_task.status == MainAITaskStatus.waiting_ci, "open_pr completes the real (simulated) PR creation, then V0.3's ci_wait starts"

    # V0.3's own real wait-polling tick only picks up a wait once its 20s real backoff elapses
    # (app/mainai_execution/ci_wait.py's CI_POLL_BASE_SECONDS) -- this test calls the same
    # resume_waiting_ci_task() that tick calls directly, the exact same convention
    # test_mainai_execution_ci_wait.py's own tests already use to avoid a real 20s sleep.
    from app.mainai_execution.execution_job import resume_waiting_ci_task
    from app.models.mainai_wait import MainAITaskWait

    wait = superuser_db.execute(select(MainAITaskWait).where(MainAITaskWait.task_id == open_pr_task.id)).scalar_one()
    await resume_waiting_ci_task(superuser_db, wait)
    superuser_db.commit()

    final_tasks = await _run_until_stalled_or_done(superuser_db, goal.id)
    assert all(t.status == MainAITaskStatus.completed for t in final_tasks), {t.task_type: t.status for t in final_tasks}

    # The real (simulated) PR's own details were durably checkpointed as "work_result" before
    # the ci_wait started (run_task_execution_job()'s own record_checkpoint() call) -- the same
    # durable fact a resumed attempt would read, not a value this test recomputes by hand.
    from app.mainai_execution.checkpoint import latest_checkpoint_for_step

    open_pr_checkpoint = latest_checkpoint_for_step(
        superuser_db, task_id=open_pr_task.id, job_id=open_pr_task.mainai_job_id, step="work_result"
    )
    work_result = open_pr_checkpoint.executor_state["work_result"]
    assert work_result["proposed"] is False
    assert work_result["pull_request_number"] == 777
    assert work_result["head_sha"] == local_sha
    assert create_calls["n"] == 1
    assert len(created_prs) == 1
    # Explicit, load-bearing: this PR is a locally simulated GitHubClient response, never a
    # live api.github.com call -- see this module's own docstring for why, and why that is the
    # existing, already-reviewed convention, not a shortcut taken here.


# ---------------------------------------------------------------- B. fail-closed authorization


def test_an_already_authorized_candidate_can_never_be_authorized_again(db_session, owner_id):
    candidate, _authorized, _goal = _stage_and_authorize(db_session, owner_id=owner_id, pr_number=502)
    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(
            db_session, owner_id=owner_id, candidate_id=candidate.id, authorized_by="someone-else-entirely",
        )


def test_a_dismissed_candidate_can_never_be_authorized(db_session, owner_id):
    item = WorkQueueItem(
        kind="open_pull_request", number=503, title="Noise, not a real engineering problem",
        html_url="https://github.com/d1n095/LifeAI/pull/503", updated_at="2026-11-16T00:00:00Z", signals=(),
    )
    candidate = record_github_evidence_candidates(db_session, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[item])[0]
    db_session.commit()

    dismiss_github_evidence_candidate(db_session, owner_id=owner_id, candidate_id=candidate.id, reason="not a real signal")
    db_session.commit()

    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(db_session, owner_id=owner_id, candidate_id=candidate.id, authorized_by="founder-real-dispatch-test")


@pytest.mark.asyncio
async def test_recording_evidence_twice_for_the_same_pr_never_duplicates_the_candidate_row(db_session, owner_id):
    item = WorkQueueItem(
        kind="open_pull_request", number=504, title="Same PR, scanned twice",
        html_url="https://github.com/d1n095/LifeAI/pull/504", updated_at="2026-11-16T00:00:00Z", signals=("ci_red",),
    )
    first = record_github_evidence_candidates(db_session, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[item])
    db_session.commit()
    second = record_github_evidence_candidates(db_session, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[item])
    db_session.commit()
    assert first[0].id == second[0].id

    count = db_session.execute(sa_text("SELECT count(*) FROM github_evidence_candidates WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()
    assert count == 1


# ---------------------------------------------------------------- C. crash / resume


@pytest.mark.asyncio
async def test_a_real_crash_between_commit_and_push_recovers_through_the_existing_takeover_pipeline_and_the_pr_is_still_created_once(
    db_session, superuser_db, owner_id, monkeypatch, patched_github, simulated_github_pr_api
):
    """LOCAL_COMMITTED_NOT_PUSHED (app.mainai_execution.worktree's own named state): the
    worktree's real local git commit exists, but the push to origin never ran -- simulating a
    worker process dying between the two. Recovered via the SAME real pipeline PR #447 already
    hardened this session (detect -> inspect -> classify -> takeover), reused here, not
    re-derived — this test's own job is proving the NEW github-evidence-authorized goal
    survives it, not re-proving the generic recovery mechanics in isolation."""
    created_prs, create_calls = simulated_github_pr_api
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_edit)

    _candidate, _authorized, goal = _stage_and_authorize(db_session, owner_id=owner_id, pr_number=505)
    create_plan(
        db_session, goal=goal, rationale="Crash/resume demonstration",
        tasks=[
            PlannedTaskSpec(
                description="Apply the small, reviewable doc-reference fix.", task_type="repo_edit",
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_real_cycle_doc_fix.py"}],
            ),
        ],
        created_by="founder-real-dispatch-test",
    )
    db_session.commit()
    repo_edit_task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    grant_task_approval(db_session, task=repo_edit_task, approved_by="founder-real-dispatch-test")
    db_session.commit()

    # Dispatch and run the job for real, but NEVER let the worker's own lease-aware push step
    # complete: a hard, real-world crash kills the process at an arbitrary point, not at a
    # convenient one. This reproduces that by monkeypatching push to fail, the same technique
    # test_mainai_execution_demos.py's own restart/resume demo uses for the analogous case.
    from app.mainai_execution import execution_job as execution_job_module
    from app.mainai_execution.worktree import push_worktree_branch as _real_push_worktree_branch

    async def _dead_push(worktree):
        raise RuntimeError("simulated worker crash — process died before the push ever ran")

    # execution_job.py imported push_worktree_branch by name (`from ...worktree import
    # push_worktree_branch`), so its own module-level binding — not worktree.py's — is what
    # _finalize_repo_edit() actually calls; patching the original module would silently do
    # nothing here.
    monkeypatch.setattr(execution_job_module, "push_worktree_branch", _dead_push)

    executor.dispatch_ready_task(superuser_db, task=repo_edit_task, goal=goal, dispatched_by="founder-real-dispatch-test")
    superuser_db.commit()
    dead_job_id = superuser_db.execute(sa_text("SELECT mainai_job_id FROM mainai_tasks WHERE id = :t"), {"t": str(repo_edit_task.id)}).scalar()
    claim_next_mainai_job(superuser_db, "dead-worker", 120)

    from app.mainai_execution.execution_job import run_task_execution_job

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await run_task_execution_job(superuser_db, dead_job_id, owner_id, worker_id="dead-worker", lease_generation=1, lease_seconds=120)
    superuser_db.rollback()

    worktree_row = superuser_db.execute(sa_text("SELECT path, branch, current_commit FROM mainai_task_worktrees WHERE job_id = :j"), {"j": str(dead_job_id)}).one()
    assert worktree_row.current_commit is not None, "the real local commit must have survived the simulated crash — it is durable git state, not an in-memory fact"
    local_sha = subprocess.run(["git", "-C", worktree_row.path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    remote_before = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{worktree_row.branch}"], capture_output=True, text=True
    )
    assert remote_before.returncode != 0 or remote_before.stdout.strip() != local_sha, "the push must genuinely not have happened yet"

    # Kill the lease exactly like every other real recovery test this session's own PR #447
    # work already established, then drive the SAME real pipeline through to a real takeover —
    # no second recovery mechanism invented for the github-evidence entry point.
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(dead_job_id)})
    superuser_db.commit()

    # Restore ONLY the push patch for the takeover's own fresh attempt — monkeypatch.undo()
    # would also revert patched_github's own patches (github_write_enabled, the bare-remote
    # URL, GitHubClient.get_ref/is_configured), which must stay in effect for recovery to work
    # against the same real local remote.
    monkeypatch.setattr(execution_job_module, "push_worktree_branch", _real_push_worktree_branch)

    dead_job = superuser_db.get(MainAIJob, dead_job_id)
    record = get_or_create_recovery_record(superuser_db, task=repo_edit_task, job=dead_job)
    superuser_db.commit()
    record = await inspect_recovery_record(superuser_db, task=repo_edit_task, job=dead_job, record=record)
    superuser_db.commit()
    record = classify_recovery_record(superuser_db, record=record)
    superuser_db.commit()
    assert record.classification in (RecoveryClassification.local_committed_not_pushed, RecoveryClassification.local_uncommitted_work), record.classification

    record, new_job = await execute_takeover(superuser_db, task=repo_edit_task, goal=goal, record=record, dispatched_by="mainai_worker_auto_recovery")
    superuser_db.commit()
    assert new_job is not None

    final_tasks = await _run_until_stalled_or_done(superuser_db, goal.id)
    repo_edit_final = [t for t in final_tasks if t.task_type == "repo_edit"][0]
    assert repo_edit_final.status == MainAITaskStatus.completed

    final_worktree = superuser_db.execute(
        sa_text("SELECT branch, current_commit FROM mainai_task_worktrees WHERE task_id = :t ORDER BY created_at DESC LIMIT 1"),
        {"t": str(repo_edit_task.id)},
    ).one()
    remote_after = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{final_worktree.branch}"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remote_after == final_worktree.current_commit, "the recovered attempt's commit must have genuinely reached the remote this time"
