"""`app.mainai_cognitive_ops.repo_backup_intelligence`. Runs REAL, read-only git commands
against a throwaway git repo built in a tmp dir (never the real project checkout, so these
tests never depend on -- or affect -- this worktree's own state). See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

import subprocess

import pytest

from app.mainai_cognitive_ops.repo_backup_intelligence import (
    GitIntrospectionError,
    assess_backup_risk,
    get_dirty_files,
    get_local_head,
    snapshot_remote_sync_state,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def bare_and_clone(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / "a.txt").write_text("hello")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "initial")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    return work


def test_clean_pushed_repo_is_not_material(bare_and_clone):
    state = snapshot_remote_sync_state(str(bare_and_clone))
    assert state.ahead == 0 and state.behind == 0
    assert state.dirty_files == ()
    risk = assess_backup_risk(state)
    assert risk.material is False


def test_local_commit_missing_on_remote_is_detected(bare_and_clone):
    (bare_and_clone / "b.txt").write_text("new work")
    _git(bare_and_clone, "add", "b.txt")
    _git(bare_and_clone, "commit", "-m", "unpushed work")

    state = snapshot_remote_sync_state(str(bare_and_clone))
    assert state.ahead == 1
    risk = assess_backup_risk(state)
    assert risk.material is True
    assert "only locally" in risk.message


def test_dirty_working_tree_is_detected(bare_and_clone):
    (bare_and_clone / "a.txt").write_text("changed, uncommitted")
    dirty = get_dirty_files(str(bare_and_clone))
    assert "a.txt" in dirty
    state = snapshot_remote_sync_state(str(bare_and_clone))
    risk = assess_backup_risk(state)
    assert risk.material is True


def test_branch_with_no_upstream_is_flagged(tmp_path):
    work = tmp_path / "lonely"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / "a.txt").write_text("x")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "c1")

    state = snapshot_remote_sync_state(str(work))
    assert state.remote_sha is None
    risk = assess_backup_risk(state)
    assert risk.material is True
    assert "no known upstream" in risk.message


def test_git_introspection_error_on_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GitIntrospectionError):
        get_local_head(str(not_a_repo))


def test_real_project_worktree_reports_a_real_sha_no_fabrication():
    import os
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    sha = get_local_head(repo_root)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
