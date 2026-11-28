"""A failed coding attempt must leave durable evidence behind.

Measured 2026-11-28 in the provider E2E: a plan wrote both files, failed its own focused test,
and the goal worktree was reset before the repair child claimed it -- so the failed code was
gone, the child re-planned from an empty tree, and the reason the first attempt failed was
unrecoverable. Founder strategy 2026-11-28 requires persistent checkpoints even for failed code
attempts. `capture_failed_attempt()` records the diff NEXT TO the worktree (never inside it,
where the reset would delete it) without changing authority or the reset contract.
"""

from __future__ import annotations

import subprocess

import pytest

from app.development_supervisor import production_worktree as wt


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def goal_worktree(tmp_path, monkeypatch):
    """A real local git worktree standing in for one goal's shared directory."""
    goal_id = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(wt, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(wt, "FAILED_ATTEMPT_ROOT", tmp_path / "failed-attempts")
    root = tmp_path / "worktrees" / goal_id
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "test"], root)
    (root / "note_tools.py").write_text("def slugify(text):\n    return text\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "base"], root)
    return goal_id, root


def test_a_failed_attempt_is_saved_outside_the_worktree(goal_worktree):
    goal_id, root = goal_worktree
    # The attempt: an edit to a tracked file plus a brand-new test file (untracked).
    (root / "note_tools.py").write_text(
        "def slugify(text: str) -> str:\n    return text.lower().replace(' ', '-')\n", encoding="utf-8"
    )
    (root / "test_note_tools.py").write_text("def test_slugify():\n    assert True\n", encoding="utf-8")

    capture = wt.capture_failed_attempt(goal_id=goal_id, task_id="aaaa-bbbb")

    assert capture is not None
    patch = wt.FAILED_ATTEMPT_ROOT / goal_id / "aaaa-bbbb.patch"
    assert capture["patch_path"] == str(patch)
    body = patch.read_text(encoding="utf-8")
    assert "replace(' ', '-')" in body  # the tracked edit
    assert "test_note_tools.py" in body  # the untracked file is part of the diff too
    assert capture["diff_bytes"] > 0
    # It lives outside the worktree, so the next reset cannot delete it.
    assert wt.FAILED_ATTEMPT_ROOT not in root.parents
    assert not (root / ".mainai-failed-attempts").exists()


def test_capture_leaves_the_worktree_exactly_as_it_was(goal_worktree):
    goal_id, root = goal_worktree
    (root / "note_tools.py").write_text("broken attempt\n", encoding="utf-8")

    wt.capture_failed_attempt(goal_id=goal_id, task_id="aaaa-bbbb")

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout
    # Still dirty and still UNSTAGED: capturing evidence is not a commit and not a stage.
    assert status.strip() == "M note_tools.py"


def test_a_clean_worktree_has_nothing_to_capture(goal_worktree):
    goal_id, _root = goal_worktree
    assert wt.capture_failed_attempt(goal_id=goal_id, task_id="aaaa-bbbb") is None


def test_a_missing_worktree_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(wt, "FAILED_ATTEMPT_ROOT", tmp_path / "failed-attempts")
    assert wt.capture_failed_attempt(goal_id="no-such-goal", task_id="x") is None
