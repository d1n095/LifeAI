"""`app/development_supervisor/production_worktree.py` -- a real, isolated LOCAL `git worktree
add` off the worker's own on-disk checkout, keyed by goal_id (not job_id, unlike
`app/mainai_execution/worktree.py`'s job-scoped model -- see this module's own docstring for
why: `SupervisorScope.repository_identity` is one fixed string per `run_supervisor()` call,
checked for exact equality against every task's own OperatorContext, so it must be
deterministic and known BEFORE any job_id exists). Deliberately no GitHub/network dependency
at all -- everything here is real local git talking to a real local repo."""

import subprocess

import pytest

from app.development_supervisor.production_worktree import (
    ensure_goal_worktree_sync,
    goal_branch_name,
    goal_worktree_path,
)


def _git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@test.local")
    _git(repo, "config", "user.name", "Worker")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.fixture(autouse=True)
def _isolate_worktree_root(tmp_path, monkeypatch):
    """Every test gets its own WORKTREE_ROOT so parallel/rerun test sessions never collide."""
    import app.development_supervisor.production_worktree as module

    monkeypatch.setattr(module, "WORKTREE_ROOT", tmp_path / "supervisor-goal-worktrees")


def test_creates_a_real_isolated_local_worktree_at_the_source_repos_head(source_repo):
    import uuid

    goal_id = uuid.uuid4()
    expected_head = _git(source_repo, "rev-parse", "HEAD")

    repo_root, base_sha, branch = ensure_goal_worktree_sync(goal_id=goal_id, source_repo_root=source_repo)

    assert repo_root == goal_worktree_path(goal_id)
    assert repo_root.is_dir()
    assert (repo_root / "README.md").is_file()
    assert base_sha == expected_head
    assert branch == goal_branch_name(goal_id)
    assert _git(repo_root, "rev-parse", "HEAD") == expected_head
    assert _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") == branch

    # The source repo itself is never written to -- worktree add only registers metadata.
    assert _git(source_repo, "status", "--porcelain") == ""


def test_reuses_the_same_worktree_on_a_second_call_reporting_its_current_head(source_repo):
    import uuid

    goal_id = uuid.uuid4()
    first_root, first_sha, first_branch = ensure_goal_worktree_sync(goal_id=goal_id, source_repo_root=source_repo)

    # Simulate a prior task's real local commit landing in the goal's shared worktree.
    (first_root / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    _git(first_root, "add", "new_file.py")
    _git(first_root, "commit", "-q", "-m", "task 1 work")
    new_head = _git(first_root, "rev-parse", "HEAD")
    assert new_head != first_sha

    second_root, second_sha, second_branch = ensure_goal_worktree_sync(goal_id=goal_id, source_repo_root=source_repo)

    assert second_root == first_root
    assert second_branch == first_branch
    assert second_sha == new_head  # reports the CURRENT head, not the original base


def test_two_different_goals_get_two_independent_worktrees(source_repo):
    import uuid

    goal_a, goal_b = uuid.uuid4(), uuid.uuid4()
    root_a, _, branch_a = ensure_goal_worktree_sync(goal_id=goal_a, source_repo_root=source_repo)
    root_b, _, branch_b = ensure_goal_worktree_sync(goal_id=goal_b, source_repo_root=source_repo)

    assert root_a != root_b
    assert branch_a != branch_b

    (root_a / "a_only.py").write_text("a = 1\n", encoding="utf-8")
    _git(root_a, "add", "a_only.py")
    _git(root_a, "commit", "-q", "-m", "goal a work")

    assert not (root_b / "a_only.py").exists()  # goal b's checkout is untouched
