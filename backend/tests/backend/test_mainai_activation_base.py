"""Activation-mode base branch / approved base SHA fail closed, and the integration branch is
protected (Python guard + DB CHECK). Legacy (non-activation) behavior is unchanged.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.mainai_execution import worktree as wt
from app.mainai_execution.worktree import (
    BASE_BRANCH,
    ActivationBaseConfigurationError,
    activation_base,
    execution_pr_base_branch,
    execution_worktree_base_branch,
    protected_execution_branches,
)
from app.models.mainai_recovery import (
    ACTIVATION_INTEGRATION_BRANCH,
    PROTECTED_BRANCHES,
    is_protected_branch,
)

APPROVED = "c" * 40


def _set(monkeypatch, **values):
    settings = get_settings()
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def _activation(monkeypatch, **overrides):
    values = dict(
        mainai_activation_mode=True,
        github_worktree_base_branch=ACTIVATION_INTEGRATION_BRANCH,
        github_pr_base_branch=ACTIVATION_INTEGRATION_BRANCH,
        mainai_activation_approved_base_sha=APPROVED,
    )
    values.update(overrides)
    _set(monkeypatch, **values)


# ---------------------------------------------------------------- legacy behavior unchanged
def test_legacy_mode_keeps_historical_fallback(monkeypatch):
    _set(monkeypatch, mainai_activation_mode=False, github_worktree_base_branch="", github_pr_base_branch="")
    assert execution_worktree_base_branch() == BASE_BRANCH
    assert execution_pr_base_branch() == BASE_BRANCH


def test_activation_mode_is_off_by_default():
    assert get_settings().model_fields["mainai_activation_mode"].default is False
    assert get_settings().model_fields["mainai_activation_approved_base_sha"].default == ""


# ---------------------------------------------------------------- activation fail-closed matrix
def test_valid_activation_configuration(monkeypatch):
    _activation(monkeypatch)
    assert activation_base() == (ACTIVATION_INTEGRATION_BRANCH, APPROVED)
    assert execution_worktree_base_branch() == ACTIVATION_INTEGRATION_BRANCH
    assert execution_pr_base_branch() == ACTIVATION_INTEGRATION_BRANCH
    assert ACTIVATION_INTEGRATION_BRANCH in protected_execution_branches()


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"github_worktree_base_branch": ""}, "GITHUB_WORKTREE_BASE_BRANCH"),
        ({"github_worktree_base_branch": "   "}, "GITHUB_WORKTREE_BASE_BRANCH"),
        ({"github_worktree_base_branch": BASE_BRANCH, "github_pr_base_branch": BASE_BRANCH}, "legacy default"),
        ({"github_worktree_base_branch": "claude/some-branch", "github_pr_base_branch": "claude/some-branch"}, "not a protected"),
        ({"github_worktree_base_branch": "main", "github_pr_base_branch": "main"}, "not a protected"),
        ({"github_pr_base_branch": ""}, "GITHUB_PR_BASE_BRANCH"),
        ({"github_pr_base_branch": "mainai/other"}, "differs"),
        ({"mainai_activation_approved_base_sha": ""}, "APPROVED_BASE_SHA"),
        ({"mainai_activation_approved_base_sha": "c" * 39}, "APPROVED_BASE_SHA"),
        ({"mainai_activation_approved_base_sha": "C" * 40}, "APPROVED_BASE_SHA"),
        ({"mainai_activation_approved_base_sha": "g" * 40}, "APPROVED_BASE_SHA"),
    ],
)
def test_activation_misconfiguration_is_a_typed_hard_failure(monkeypatch, overrides, needle):
    _activation(monkeypatch, **overrides)
    with pytest.raises(ActivationBaseConfigurationError, match=needle):
        activation_base()
    with pytest.raises(ActivationBaseConfigurationError):
        execution_worktree_base_branch()
    with pytest.raises(ActivationBaseConfigurationError):
        execution_pr_base_branch()


def test_activation_error_is_a_worktree_error_so_existing_handlers_fail_closed():
    assert issubclass(ActivationBaseConfigurationError, wt.WorktreeError)


@pytest.mark.asyncio
async def test_moved_integration_tip_is_a_no_go_before_any_filesystem_effect(monkeypatch, tmp_path):
    _activation(monkeypatch, github_write_enabled=True, github_repo="d1n095/LifeAI", github_token="fake")
    monkeypatch.setattr(wt, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    async def _tip(self, branch):
        assert branch == ACTIVATION_INTEGRATION_BRANCH
        return "d" * 40

    monkeypatch.setattr(GitHubClient, "get_ref", _tip)
    called = []
    monkeypatch.setattr(wt, "create_task_worktree_sync", lambda *a, **k: called.append(k))
    with pytest.raises(ActivationBaseConfigurationError, match="not the approved base SHA"):
        await wt.create_task_worktree(None, task=None, job=None, lease_generation=1, executor_id="x")
    assert called == [] and not (tmp_path / "worktrees").exists()


@pytest.mark.asyncio
async def test_unmoved_tip_builds_from_the_approved_sha(monkeypatch):
    _activation(monkeypatch, github_write_enabled=True, github_repo="d1n095/LifeAI", github_token="fake")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    async def _tip(self, branch):
        return APPROVED

    monkeypatch.setattr(GitHubClient, "get_ref", _tip)
    seen = {}
    monkeypatch.setattr(wt, "create_task_worktree_sync", lambda db, **k: seen.update(k) or "ok")
    assert await wt.create_task_worktree(None, task=None, job=None, lease_generation=1, executor_id="x") == "ok"
    assert seen["base_sha"] == APPROVED


@pytest.mark.asyncio
async def test_activation_misconfiguration_stops_worktree_creation(monkeypatch):
    _activation(monkeypatch, github_write_enabled=True, github_repo="d1n095/LifeAI", github_token="fake",
                github_worktree_base_branch="")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    async def _tip(self, branch):  # must never be reached
        raise AssertionError("no network call on a misconfigured activation base")

    monkeypatch.setattr(GitHubClient, "get_ref", _tip)
    with pytest.raises(ActivationBaseConfigurationError):
        await wt.create_task_worktree(None, task=None, job=None, lease_generation=1, executor_id="x")


def test_sync_path_refuses_a_base_sha_other_than_the_approved_one(db_session, monkeypatch, tmp_path):
    _activation(monkeypatch, github_write_enabled=True, github_repo="d1n095/LifeAI", github_token="fake")
    monkeypatch.setattr(wt, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    class _Job:
        id = uuid.uuid4()

    class _Task:
        id = uuid.uuid4()
        owner_id = uuid.uuid4()

    with pytest.raises(ActivationBaseConfigurationError, match="Founder-approved"):
        wt.create_task_worktree_sync(db_session, task=_Task(), job=_Job(), lease_generation=1, executor_id="x", base_sha="e" * 40)
    assert not (tmp_path / "worktrees").exists()


# ---------------------------------------------------------------- protected integration branch
@pytest.mark.parametrize(
    "name, protected",
    [
        (ACTIVATION_INTEGRATION_BRANCH, True),
        ("mainai/release-founder-alpha", True),
        ("mainai/anything/else", True),
        ("main", True),
        ("master", True),
        ("claude/det-kommer-mer-879lcm", True),
        ("claude/mainai-task-6f1c2a9e-0000-4000-8000-000000000000", False),
        ("mainaix/foo", False),
        ("", False),
        (None, False),
    ],
)
def test_is_protected_branch(name, protected):
    assert is_protected_branch(name) is protected


def test_integration_branch_is_listed_explicitly():
    assert ACTIVATION_INTEGRATION_BRANCH in PROTECTED_BRANCHES


@pytest.fixture
def _worktree_row_factory(superuser_db, make_verified_user):
    from app.mainai_execution import planner
    from app.mainai_execution.planner import PlannedTaskSpec
    from app.models.mainai_execution import MainAITask

    user, _ = make_verified_user()
    db = superuser_db
    db.execute(sa_text("SELECT set_config('app.current_user_id', :u, false)"), {"u": str(user.id)})
    goal = planner.create_goal(db, owner_id=user.id, title="g", original_instruction="x", created_by="test")
    planner.create_plan(db, goal=goal, rationale="r", created_by="test",
                        tasks=[PlannedTaskSpec(description="a", task_type="read_only_audit", verification_plan=[])])
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    from app.mainai_execution import executor

    job = executor.dispatch_ready_task(db, task=task, goal=goal, dispatched_by="test")
    db.commit()

    def _insert(branch: str):
        db.execute(
            sa_text(
                "INSERT INTO mainai_task_worktrees (id, task_id, owner_id, job_id, lease_generation, executor_id, repo, base_sha, branch, path, marker_token, status, created_at) "
                "VALUES (CAST(:id AS uuid), CAST(:task AS uuid), CAST(:owner AS uuid), CAST(:job AS uuid), 1, 'x', 'd1n095/LifeAI', :sha, :branch, '/tmp/x', 'tok', 'active', :now)"
            ),
            {"id": str(uuid.uuid4()), "task": str(task.id), "owner": str(user.id), "sha": "a" * 40, "branch": branch,
             "now": datetime.utcnow(), "job": str(job.id)},
        )

    return db, _insert


@pytest.mark.parametrize("branch", [ACTIVATION_INTEGRATION_BRANCH, "mainai/release-founder-alpha", "main", "claude/det-kommer-mer-879lcm"])
def test_db_check_constraint_rejects_protected_branches(_worktree_row_factory, branch):
    db, insert = _worktree_row_factory
    with pytest.raises(IntegrityError, match="ck_mainai_task_worktrees_branch_not_protected"):
        insert(branch)
    db.rollback()


def test_db_check_constraint_accepts_a_task_branch(_worktree_row_factory):
    db, insert = _worktree_row_factory
    insert("claude/mainai-task-6f1c2a9e-0000-4000-8000-000000000000")
    db.rollback()
