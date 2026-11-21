"""M4 — a proposal is written into its own checkout, never the tree the deployment runs.

Before this, proposal mode (`GITHUB_WRITE_ENABLED` off) wrote AI content straight into the single
checkout the backend process itself executes from. PI-1d made that failure recoverable (the tree is
restored on any non-accepting path), but recoverability is not isolation: a killed process inside
the write→rollback window leaves an unaccepted proposal in the code the next startup runs.

`app/mainai_execution/proposal_worktree.py` closes the window: every attempt gets a local-only
checkout built from the shared checkout's HEAD by a filesystem-path fetch, with no remote at all.
These tests prove both halves of that claim:

  * the bootstrap carries commits but no remote, no URL and no token (a shared checkout whose
    `origin` embeds a PAT is the exact shape a real deployment has);
  * a failing proposal leaves the shared checkout byte-identical and clean, and the verification
    step that judged it ran inside the isolated tree — not against the live code;
  * when isolation genuinely cannot be built, the code falls back to the shared checkout and
    PI-1d's rollback still restores it (defense in depth, still verified).
"""

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner, proposal_worktree
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.proposal_worktree import (
    ProposalWorktreeError,
    assert_no_credentialed_remote,
    bootstrap_local_base,
    create_isolated_proposal_checkout,
    remote_urls,
    remove_isolated_proposal_checkout,
)
from app.mainai_execution.verify import VerificationResult, VerificationStepResult
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var

_TOKEN = "ghp_PROPOSAL_ISOLATION_REGRESSION_TOKEN"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _shared_checkout(tmp_path: Path, *, remote_url: str | None = None) -> Path:
    shared = tmp_path / "shared"
    shared.mkdir()
    _git(["init", "-q", "."], shared)
    _git(["config", "user.email", "test@lifeai.invalid"], shared)
    _git(["config", "user.name", "Test"], shared)
    (shared / "README.md").write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    _git(["add", "-A"], shared)
    _git(["commit", "-q", "-m", "base"], shared)
    if remote_url is not None:
        _git(["remote", "add", "origin", remote_url], shared)
    return shared


def _files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file():
            try:
                if needle.encode() in path.read_bytes():
                    hits.append(str(path))
            except OSError:  # pragma: no cover - defensive
                continue
    return hits


def test_the_bootstrap_carries_commits_but_no_remote_and_no_token(tmp_path, monkeypatch):
    """The real deployment shape: the shared checkout's `origin` embeds a PAT (worktree.py's
    `_authed_remote_url`). The isolated base must contain its commits and none of that URL."""
    monkeypatch.setattr(proposal_worktree, "PROPOSAL_ROOT", tmp_path / "proposal-root")
    shared = _shared_checkout(
        tmp_path, remote_url=f"https://x-access-token:{_TOKEN}@github.com/d1n095/LifeAI.git"
    )
    head = _git(["rev-parse", "HEAD"], shared).stdout.strip()

    dest = tmp_path / "base.git"
    base_sha = bootstrap_local_base(shared, dest=dest)

    assert base_sha == head, "the bootstrap must pin the shared checkout's actual HEAD"
    assert remote_urls(dest) == [], "a proposal base must have no remote at all"
    assert _files_containing(dest, _TOKEN) == [], "no file under the proposal base may contain the PAT"
    assert_no_credentialed_remote(dest)  # does not raise

    checkout = create_isolated_proposal_checkout(shared_checkout=shared, attempt_id=uuid.uuid4())
    try:
        assert (checkout.tree / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
        assert remote_urls(checkout.tree) == []
        assert _files_containing(checkout.root, _TOKEN) == []
        attempt = json.loads((checkout.root / "attempt.json").read_text(encoding="utf-8"))
        assert attempt["base_sha"] == head
    finally:
        remove_isolated_proposal_checkout(checkout)
    assert not checkout.root.exists()


def test_a_credentialed_remote_is_rejected_rather_than_trusted(tmp_path):
    """The guard itself, independent of how a remote got there: a URL with a scheme or an `@` is
    never an acceptable remote for a proposal checkout."""
    repo = tmp_path / "credentialed"
    repo.mkdir()
    _git(["init", "-q", "."], repo)
    _git(["remote", "add", "origin", f"https://x-access-token:{_TOKEN}@github.com/o/r.git"], repo)

    with pytest.raises(ProposalWorktreeError):
        assert_no_credentialed_remote(repo)


def test_a_shared_checkout_without_git_fails_closed_rather_than_silently(tmp_path):
    """No `.git`, no bootstrap. The caller needs a real error to decide whether to fall back —
    never an empty directory that would be verified as if it were code."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(ProposalWorktreeError):
        bootstrap_local_base(not_a_repo, dest=tmp_path / "base.git")


# ---------------------------------------------------------------------------------------------
# End-to-end through the real job path (real Postgres; only the provider and the verdict faked)
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _isolate_proposal_root(tmp_path, monkeypatch):
    """Every test's attempts land under its own tmp_path, so a leftover directory can never make
    a later test pass by accident."""
    monkeypatch.setattr(proposal_worktree, "PROPOSAL_ROOT", tmp_path / "proposal-root")


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _proposal(*files: tuple[str, str]) -> str:
    return json.dumps(
        {"files": [{"path": p, "content": c} for p, c in files], "commit_message": "proposal"}
    )


def _failing_verification(*_args, **_kwargs) -> VerificationResult:
    return VerificationResult(
        passed=False, steps=[VerificationStepResult(kind="targeted_tests", passed=False, detail={})]
    )


def _passing_verification(*_args, **_kwargs) -> VerificationResult:
    return VerificationResult(passed=True, steps=[])


async def _run_proposal(db_session, superuser_db, owner_id, monkeypatch, repo_root, response_text, verification):
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat(response_text))
    monkeypatch.setattr(execution_job, "verify_task", verification)

    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Proposal isolation",
        original_instruction="Propose an edit.",
        created_by="test",
        approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single repo_edit",
        tasks=[PlannedTaskSpec(description="propose an edit", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(
        db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120
    )
    return superuser_db.get(MainAITask, task.id)


@pytest.mark.asyncio
async def test_a_failing_proposal_never_touches_the_shared_checkout(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    shared = _shared_checkout(tmp_path)
    seen: dict = {}

    def _verification(*args, cwd=None, **kwargs):
        seen["cwd"] = str(cwd)
        seen["readme"] = (Path(cwd).parent / "README.md").read_text(encoding="utf-8")
        seen["created"] = (Path(cwd).parent / "new_area" / "proposal.md").read_text(encoding="utf-8")
        return _failing_verification()

    task = await _run_proposal(
        db_session, superuser_db, owner_id, monkeypatch, shared,
        _proposal(("README.md", "PROPOSED CONTENT\n"), ("new_area/proposal.md", "BRAND NEW\n")),
        _verification,
    )

    # The proposal was really written — into the isolated tree, which is what got verified.
    verified_root = Path(seen["cwd"]).parent
    assert verified_root not in shared.parents and verified_root != shared
    assert shared not in Path(seen["cwd"]).parents, "verification must not run inside the shared checkout"
    assert verified_root.name == "tree"
    assert seen["readme"] == "PROPOSED CONTENT\n"
    assert seen["created"] == "BRAND NEW\n"

    # ...and the live checkout is byte-identical, with nothing left behind.
    assert (shared / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert not (shared / "new_area").exists()
    assert _git(["status", "--porcelain"], shared).stdout.strip() == ""
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)


@pytest.mark.asyncio
async def test_an_accepted_proposal_also_leaves_the_shared_checkout_untouched(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Isolation must not depend on the verdict. An accepted proposal is still a PROPOSAL: it
    never lands in the code the deployment executes."""
    shared = _shared_checkout(tmp_path)

    task = await _run_proposal(
        db_session, superuser_db, owner_id, monkeypatch, shared,
        _proposal(("README.md", "ACCEPTED CONTENT\n")),
        _passing_verification,
    )

    assert (shared / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert _git(["status", "--porcelain"], shared).stdout.strip() == ""
    assert task.status == MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_an_accepted_proposals_checkout_is_removed_once_the_goal_finalizes(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """PI-1d cleanup requirement (merge reconciliation finding): before this fix, nothing ever
    called `remove_isolated_proposal_checkout()` on the SUCCESS path at all -- only
    `_propose_repo_edit()`'s own write-failure handler did. A completed proposal's isolated
    checkout would accumulate under `/tmp` forever. `final_report.py`'s `record_final_report()`
    (invoked automatically by `run_task_execution_job()`'s own finalization, exercised here end
    to end, not called directly) now removes it once the owning GOAL -- not just this one task
    -- is confirmed genuinely terminal, via the task's own durable `proposal_worktree_path`
    record."""
    shared = _shared_checkout(tmp_path)
    seen: dict = {}

    def _verification(*args, cwd=None, **kwargs):
        # Captured DURING verification, not read back afterwards: a single-task goal's last
        # task completing finalizes the GOAL inline in the same call (found reconciling this
        # test -- `_run_proposal` returning with task.status == completed already means
        # cleanup has already run), so by the time `_run_proposal` returns there is nothing
        # left to sanity-check the checkout's existence against.
        # `cwd` is the backend/ subdirectory inside the checkout's "tree" worktree (see
        # execution_job.py's `verify_cwd = str(Path(source_root) / "backend")`) -- a path
        # constructed unconditionally, whether or not this fixture's synthetic single-file
        # repo actually has a `backend/` subdirectory (it doesn't), so `cwd` itself need not
        # exist. `cwd.parent` is "tree" itself (== the task's durable `proposal_worktree_path`,
        # the directory that genuinely was created), and `cwd.parent.parent` is the whole
        # per-attempt root this test needs gone afterwards.
        seen["cwd"] = Path(cwd)
        assert seen["cwd"].parent.exists(), "sanity: the checkout really was created for this attempt"
        return _passing_verification()

    task = await _run_proposal(
        db_session, superuser_db, owner_id, monkeypatch, shared,
        _proposal(("README.md", "ACCEPTED CONTENT\n")),
        _verification,
    )
    assert task.status == MainAITaskStatus.completed

    attempt_root = seen["cwd"].parent.parent
    assert not attempt_root.exists(), "the whole per-attempt checkout directory must be gone once the goal is terminal"


@pytest.mark.asyncio
async def test_a_real_crash_right_after_the_proposal_write_never_touches_the_shared_checkout(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """PI-1d, the directive-required crash-simulation proof (ported from PR #469, which built
    it against the since-discarded copy-based isolation mechanism): a genuine process crash
    (an uncaught exception escaping `run_task_execution_job()` entirely, simulating a killed
    worker) happening AFTER `_propose_repo_edit()` has already written the proposed content into
    its isolated checkout must still never have touched the shared checkout -- recovery of a
    crashed proposal-mode attempt is "the shared tree was never at risk in the first place", not
    "something to roll back"."""
    from app.mainai_execution import execution_job

    shared = _shared_checkout(tmp_path)
    real_renew = execution_job.renew_mainai_job_lease
    call_count = {"n": 0}

    def _renew_then_crash_on_second_call(*args, **kwargs):
        call_count["n"] += 1
        real_renew(*args, **kwargs)  # the real lease-renewal write happens first, every time
        if call_count["n"] == 2:
            # Call #1 is the dispatch-time renewal, BEFORE _propose_repo_edit() ever runs --
            # crashing there would prove nothing about a crash mid-proposal, since the
            # checkout wouldn't exist yet. Call #2 is the renewal immediately after
            # _propose_repo_edit() has already run and written into its isolated checkout,
            # right before the work_result checkpoint commits -- exactly the crash point this
            # test needs.
            raise RuntimeError("simulated process crash right after the work_result checkpoint's own lease check")

    monkeypatch.setattr(execution_job, "renew_mainai_job_lease", _renew_then_crash_on_second_call)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await _run_proposal(
            db_session, superuser_db, owner_id, monkeypatch, shared,
            _proposal(("README.md", "PROPOSED CONTENT\n")),
            _passing_verification,
        )
    db_session.rollback()

    assert (shared / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n", (
        "a crash mid-proposal must never have touched the shared checkout"
    )
    assert _git(["status", "--porcelain"], shared).stdout.strip() == ""


@pytest.mark.asyncio
async def test_when_isolation_cannot_be_built_pi1d_rollback_still_protects_the_checkout(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Defense in depth: the fallback path is not "unprotected", and it is not silent either."""
    from app.mainai_execution import execution_job

    shared = _shared_checkout(tmp_path)

    def _no_bootstrap(**_kwargs):
        raise ProposalWorktreeError("git is not available in this container")

    monkeypatch.setattr(execution_job, "create_isolated_proposal_checkout", _no_bootstrap)

    task = await _run_proposal(
        db_session, superuser_db, owner_id, monkeypatch, shared,
        _proposal(("README.md", "PROPOSED CONTENT\n"), ("new_area/proposal.md", "BRAND NEW\n")),
        _failing_verification,
    )

    assert (shared / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert not (shared / "new_area").exists()
    assert _git(["status", "--porcelain"], shared).stdout.strip() == ""
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
