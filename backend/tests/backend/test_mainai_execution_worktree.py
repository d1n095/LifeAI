"""V0.2 worktree isolation (app/mainai_execution/worktree.py) — the founder-approved
prerequisite for real dead-agent recovery. Uses a REAL local bare git repository as the
GitHub remote stand-in (no fake filesystem, no mocked git subprocess calls) — only
`GitHubClient.get_ref()` and the remote URL construction are patched, so `git`
itself does every real operation (init, fetch, checkout, commit, push) exactly as it would
against the real GitHub remote.

Covers:
  A. create_task_worktree(): real isolated checkout from a verified base SHA, task-scoped
     branch, marker file written and matching the DB row; idempotent re-call for the same job.
  B. Fails closed (no directory left behind) when GitHub write is not configured/enabled.
  C. verify_worktree_ownership(): true right after creation, false after any tamper
     (mismatched token, mismatched task_id, deleted marker file).
  D. commit_worktree_changes(): None when nothing changed; a real local commit SHA when
     something did, with `current_commit` durably updated; refuses to run against an
     unverified worktree.
  E. push_worktree_branch(): a real non-force push that lands on the bare repo, verified via
     the (patched) GitHub read-side; a genuine remote divergence is rejected as
     WorktreePushRejected, never force-pushed.
  F. release_worktree(): status/released_at set, directory actually removed from disk.
  G. task_branch_name() never produces a protected/mainline branch name (drift guard)."""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient
from app.mainai_execution import executor, planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.worktree import (
    BASE_BRANCH,
    WorktreeError,
    WorktreeOwnershipError,
    WorktreePushRejected,
    commit_worktree_changes,
    create_task_worktree,
    push_worktree_branch,
    rebind_worktree_to_job,
    release_worktree,
    task_branch_name,
    verify_worktree_ownership,
    worktree_git_status,
)
from app.models.mainai_execution import MainAITask
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import PROTECTED_BRANCHES, MainAITaskWorktreeStatus
from app.request_context import current_user_id as current_user_id_var


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
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


@pytest.fixture
def bare_remote(tmp_path) -> Path:
    """A real bare git repo standing in for GitHub -- `git init --bare` plus one commit on
    BASE_BRANCH, exactly like the real `claude/det-kommer-mer-879lcm` base this system targets."""
    remote_path = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_path)], check=True)

    seed_path = tmp_path / "seed-clone"
    subprocess.run(["git", "clone", "-q", str(remote_path), str(seed_path)], check=True)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_path), "config", "user.email", "seed@test.local"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "checkout", "-q", "-b", BASE_BRANCH], check=True)
    subprocess.run(["git", "-C", str(seed_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "push", "-q", "origin", BASE_BRANCH], check=True)
    return remote_path


@pytest.fixture
def patched_github(monkeypatch, bare_remote):
    """Points worktree.py's remote-URL construction and GitHubClient.get_ref() at the real
    local bare repo instead of github.com/a real token -- everything downstream (git init,
    fetch, checkout, commit, push) is real git talking to a real (local) remote."""
    from app.config import get_settings
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(bare_remote))

    async def _fake_get_ref(self, branch: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(bare_remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    return bare_remote


def _goal(db_session, owner_id):
    return planner.create_goal(
        db_session, owner_id=owner_id, title="Worktree test goal",
        original_instruction="Edit a file.", created_by="test",
    )


def _repo_edit_job(db_session, owner_id) -> tuple[MainAITask, MainAIJob]:
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="single repo_edit task",
        tasks=[PlannedTaskSpec(description="edit a file", task_type="repo_edit")], created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    return task, job


# ---------------------------------------------------------------- A/B. creation


@pytest.mark.asyncio
async def test_create_task_worktree_is_real_isolated_checkout_at_verified_base_sha(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)

    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    assert wt.branch == task_branch_name(task.id) == f"claude/mainai-task-{task.id}"
    assert wt.branch not in PROTECTED_BRANCHES
    assert wt.repo == "test-owner/test-repo"
    assert wt.status == MainAITaskWorktreeStatus.active
    assert Path(wt.path).is_dir()
    assert (Path(wt.path) / ".mainai_worktree_owner.json").is_file()

    # Base SHA matches what the (patched) GitHub read-side actually reports for BASE_BRANCH.
    expected_base_sha = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{BASE_BRANCH}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert wt.base_sha == expected_base_sha

    # Local checkout really is on the task branch, at that exact SHA.
    head = subprocess.run(["git", "-C", wt.path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert head == expected_base_sha


@pytest.mark.asyncio
async def test_create_task_worktree_is_idempotent_for_the_same_job(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    first = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    second = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    assert second.id == first.id


@pytest.mark.asyncio
async def test_create_task_worktree_fails_closed_when_github_write_disabled(db_session, owner_id, monkeypatch, bare_remote):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "github_write_enabled", False)
    task, job = _repo_edit_job(db_session, owner_id)

    with pytest.raises(WorktreeError):
        await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")

    from app.models.mainai_recovery import MainAITaskWorktree
    assert db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).one_or_none() is None


# ---------------------------------------------------------------- C. ownership verification


@pytest.mark.asyncio
async def test_verify_worktree_ownership_detects_tampering(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    assert verify_worktree_ownership(wt) is True

    marker_path = Path(wt.path) / ".mainai_worktree_owner.json"
    original = marker_path.read_text(encoding="utf-8")

    marker_path.write_text(original.replace(wt.marker_token, "not-the-real-token"), encoding="utf-8")
    assert verify_worktree_ownership(wt) is False

    marker_path.write_text(original, encoding="utf-8")
    assert verify_worktree_ownership(wt) is True

    marker_path.unlink()
    assert verify_worktree_ownership(wt) is False


@pytest.mark.asyncio
async def test_verify_worktree_ownership_rejects_a_different_tasks_marker(db_session, owner_id, patched_github):
    """Simulates the container-replacement scenario: a stale directory happens to exist at a
    reused path but belongs to a DIFFERENT task/job -- must never be trusted."""
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    wt.task_id = uuid.uuid4()  # in-memory only -- simulates comparing against the wrong row
    assert verify_worktree_ownership(wt) is False


@pytest.mark.asyncio
async def test_rebind_fences_a_stale_workers_own_in_memory_worktree_object(db_session, owner_id, patched_github):
    """Hardening-pass attack (section 3, 'stale worker returns'): worktree.py grants no lease
    of its own (see its module docstring) -- ownership is entirely the marker-token comparison.
    Proves that ALONE is enough to fence a stale worker A that still holds its OWN in-memory
    MainAITaskWorktree object (loaded before a takeover rebinds the row) from writing to the
    SAME on-disk directory after rebind_worktree_to_job() has handed it to a new attempt: A's
    object still carries the OLD marker_token, but the on-disk marker file was overwritten with
    a freshly minted one during rebind -- the comparison in verify_worktree_ownership() reads
    A's stale in-memory value, so it can never match the new on-disk secret."""
    task, job = _repo_edit_job(db_session, owner_id)
    stale_worktree_object = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-A")
    db_session.commit()
    old_marker_token = stale_worktree_object.marker_token
    assert verify_worktree_ownership(stale_worktree_object) is True

    new_job = MainAIJob(
        owner_id=owner_id, job_type="task_execution", status="queued", input_refs={}, created_by="test",
    )
    db_session.add(new_job)
    db_session.flush()
    rebind_worktree_to_job(db_session, stale_worktree_object, new_job_id=new_job.id, new_lease_generation=1)
    db_session.commit()

    # The rebind mutated the SAME Python object stale_worktree_object points at (rebind_worktree_to_job
    # returns and mutates it in place) -- simulate worker A instead holding its OWN separate,
    # pre-rebind snapshot, exactly like a real stale process that loaded the row before rebind
    # and never refreshed it.
    class _StaleSnapshot:
        pass

    stale_snapshot = _StaleSnapshot()
    stale_snapshot.task_id = task.id
    stale_snapshot.job_id = job.id  # worker A's own OLD job_id, from before the rebind
    stale_snapshot.marker_token = old_marker_token
    stale_snapshot.path = stale_worktree_object.path

    assert verify_worktree_ownership(stale_snapshot) is False  # worker A can never write again
    with pytest.raises(WorktreeOwnershipError):
        commit_worktree_changes(db_session, stale_snapshot, message="worker A trying to write after being fenced")

    # The NEW attempt's own (rebound) object is correctly trusted.
    assert verify_worktree_ownership(stale_worktree_object) is True
    assert stale_worktree_object.job_id == new_job.id
    assert stale_worktree_object.marker_token != old_marker_token


# ---------------------------------------------------------------- D. local commit


@pytest.mark.asyncio
async def test_commit_worktree_changes_returns_none_when_nothing_changed(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    assert commit_worktree_changes(db_session, wt, message="no-op") is None
    assert wt.current_commit is None


@pytest.mark.asyncio
async def test_commit_worktree_changes_creates_a_real_local_commit(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    (Path(wt.path) / "new_file.txt").write_text("hello from mainai\n", encoding="utf-8")
    sha = commit_worktree_changes(db_session, wt, message="add new_file.txt")
    db_session.commit()

    assert sha is not None
    assert wt.current_commit == sha
    status = worktree_git_status(wt)
    assert status["ownership_verified"] is True
    assert status["has_uncommitted_changes"] is False
    assert status["local_head_sha"] == sha

    # A real, independently addressable git commit object -- verifiable with zero network.
    cat_file = subprocess.run(["git", "-C", wt.path, "cat-file", "-t", sha], capture_output=True, text=True)
    assert cat_file.stdout.strip() == "commit"


@pytest.mark.asyncio
async def test_commit_worktree_changes_refuses_an_unverified_worktree(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    (Path(wt.path) / "x.txt").write_text("x", encoding="utf-8")
    (Path(wt.path) / ".mainai_worktree_owner.json").unlink()

    with pytest.raises(WorktreeOwnershipError):
        commit_worktree_changes(db_session, wt, message="should not run")


# ---------------------------------------------------------------- E. push


@pytest.mark.asyncio
async def test_push_worktree_branch_lands_a_real_non_force_push(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    (Path(wt.path) / "pushed.txt").write_text("real content\n", encoding="utf-8")
    local_sha = commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()

    remote_sha = await push_worktree_branch(wt)
    assert remote_sha == local_sha

    actual_remote_sha = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{wt.branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert actual_remote_sha == local_sha


@pytest.mark.asyncio
async def test_push_worktree_branch_rejects_real_divergence_never_force_pushes(db_session, owner_id, patched_github, tmp_path):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    (Path(wt.path) / "mine.txt").write_text("mine\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="mine.txt")
    db_session.commit()

    # A second, independent clone pushes a DIFFERENT commit to the same branch first --
    # simulates a takeover/resume race where the remote moved out from under this worktree.
    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(patched_github), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", wt.branch, wt.base_sha], check=True)
    (other_clone / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "other@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "add", "theirs.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", wt.branch], check=True)

    remote_sha_before = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{wt.branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    with pytest.raises(WorktreePushRejected):
        await push_worktree_branch(wt)

    # Rejected, not force-pushed -- the other clone's commit is still the remote tip.
    remote_sha_after = subprocess.run(
        ["git", "-C", str(patched_github), "rev-parse", f"refs/heads/{wt.branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_sha_after == remote_sha_before


# ---------------------------------------------------------------- F. release


@pytest.mark.asyncio
async def test_release_worktree_marks_status_and_removes_directory(db_session, owner_id, patched_github):
    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    path = Path(wt.path)
    assert path.is_dir()

    release_worktree(db_session, wt, status=MainAITaskWorktreeStatus.released)
    db_session.commit()

    assert wt.status == MainAITaskWorktreeStatus.released
    assert wt.released_at is not None
    assert not path.exists()


# ---------------------------------------------------------------- G. drift guard


def test_task_branch_name_never_collides_with_a_protected_branch():
    for _ in range(20):
        name = task_branch_name(uuid.uuid4())
        assert name not in PROTECTED_BRANCHES
        assert name.startswith("claude/mainai-task-")


# ---------------------------------------------------------------- H. PROV-1 — no persisted
# token (founder "RESUME LIFEAI FIRST-CYCLE REMEDIATION" directive, Freebuff finding PROV-1:
# GitHub write credentials stored in worktree remotes)


def test_authed_remote_url_never_embeds_the_real_token():
    """Direct regression on the vulnerable line itself: `git remote add origin <this URL>`
    persists its return value VERBATIM into the worktree's own `.git/config` on disk for the
    worktree's entire lifetime. If a future change reverts to embedding the token in this
    URL (e.g. `https://x-access-token:{token}@github.com/...`), this test fails immediately."""
    from app.mainai_execution import worktree as worktree_module

    url = worktree_module._authed_remote_url("some-owner/some-repo", "ghp_RealSecretTokenValue")
    assert "ghp_RealSecretTokenValue" not in url
    assert url == "https://github.com/some-owner/some-repo.git"


def test_auth_header_args_carries_the_token_only_as_a_transient_cli_flag():
    """The real token must still reach GitHub somehow -- via a `-c http.extraheader=...` flag
    applied only to the one network call that needs it, never persisted to any config file."""
    import base64

    from app.mainai_execution import worktree as worktree_module

    args = worktree_module._auth_header_args("ghp_RealSecretTokenValue")
    assert args[0] == "-c"
    key, _, value = args[1].partition("=")
    assert key == "http.extraheader"
    scheme, _, encoded = value.partition(" basic ")
    assert base64.b64decode(encoded).decode("utf-8") == "x-access-token:ghp_RealSecretTokenValue"


@pytest.mark.asyncio
async def test_create_task_worktree_never_passes_the_token_to_remote_add(db_session, owner_id, patched_github, monkeypatch):
    """End-to-end call-site proof: even with the REAL (unpatched) `_auth_header_args` wired
    into `create_task_worktree_sync()`, the real `settings.github_token` value never appears
    in the argv of the `git remote add` call -- only in the `git fetch` call's transient
    `-c http.extraheader=...` flag, which `remote add` never sees and `.git/config` never
    retains. Spies on `_run_git` (delegating to the real implementation) rather than faking
    git entirely, so every other operation (init, fetch, checkout) still really happens
    against the local bare-repo stand-in."""
    from app.config import get_settings
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    real_token = "ghp_RealSecretTokenValue_should_never_reach_remote_add"
    monkeypatch.setattr(settings, "github_token", real_token)

    recorded_calls: list[list[str]] = []
    real_run_git = worktree_module._run_git

    def _spying_run_git(args, **kwargs):
        recorded_calls.append(list(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(worktree_module, "_run_git", _spying_run_git)

    task, job = _repo_edit_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()

    import base64

    remote_add_calls = [c for c in recorded_calls if c[:2] == ["remote", "add"]]
    fetch_calls = [c for c in recorded_calls if "fetch" in c]
    assert remote_add_calls, "expected create_task_worktree_sync to have called 'git remote add'"
    assert fetch_calls, "expected create_task_worktree_sync to have called 'git fetch'"
    for call in remote_add_calls:
        assert real_token not in call
        assert not any(arg.startswith("http.extraheader=") for arg in call)

    def _decoded_basic_auth(extraheader_arg: str) -> str:
        _, _, encoded = extraheader_arg.partition(" basic ")
        return base64.b64decode(encoded).decode("utf-8")

    decoded_fetch_headers = [
        _decoded_basic_auth(arg)
        for call in fetch_calls
        for arg in call
        if arg.startswith("http.extraheader=")
    ]
    assert decoded_fetch_headers, "expected the fetch call to carry a -c http.extraheader=... flag"
    assert any(real_token in header for header in decoded_fetch_headers), (
        "the real token must still reach the fetch call somehow (base64-encoded in the "
        "transient http.extraheader flag) -- if this assertion fails, auth was dropped "
        "entirely, not just made non-persistent"
    )

    # The one artifact that actually persists across the worktree's lifetime never contains it.
    git_config_text = (Path(wt.path) / ".git" / "config").read_text(encoding="utf-8")
    assert real_token not in git_config_text

    import stat

    mode = stat.S_IMODE(Path(wt.path).stat().st_mode)
    assert mode == 0o700
