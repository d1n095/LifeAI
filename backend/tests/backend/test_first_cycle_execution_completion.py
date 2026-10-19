"""Execution-complete successor of #290: real FO-7 worktree base-fetch, FO-6 salvage, rehearsal.

No GO claim. Codex transport/grants stay FAIL/BLOCKED. No merge/deploy/Recall.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_TESTS = str(Path(__file__).resolve().parent)
if _BACKEND_TESTS not in sys.path:
    sys.path.insert(0, _BACKEND_TESTS)
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner, replan as replan_mod
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.first_cycle_ops import run_founder_supervised_local_dry_run
from app.mainai_execution.first_cycle_stop import (
    STATE_EXTERNAL_BOUNDARY_FAILED,
    STATE_STOP_BEFORE_MERGE,
    classify_publication_failure,
    should_suppress_paid_replan,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.publication_errors import (
    ApprovedBaseMovedError,
    PublicationNetworkError,
    PublicationTimeoutError,
)
from app.mainai_execution.replan import ReplanSuppressedError, find_replan_trigger, trigger_replan
from app.mainai_execution.worktree import (
    WorktreeError,
    WorktreeTimeoutError,
    create_task_worktree,
    fetch_origin_base_sha,
)
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_recovery import RecoveryClassification
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from first_cycle_acceptance._git import seed_isolated_repo
from first_cycle_acceptance._lifecycle import run_rehearsal
from first_cycle_acceptance._matrix import FAIL, PASS
from first_cycle_acceptance.conftest import REMAINING_OUT_OF_SCOPE, conclude

from app.mainai_execution.recovery_classifier import _classify

FROZEN_289 = "ca60ba54febb49e1c7585fb776811564d54e3d21"
PARENT_290 = "6c988148a7e30531fac21e19c044f5570d95f501"
THIS_BRANCH = "cursor/first-cycle-execution-completion-91c1"
LOCAL_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# _lifecycle.EXPECTED_STAGES lives in the e2e test module; keep a local copy in lockstep.
HARNESS_STAGES = [
    "goal",
    "founder_authorization",
    "planning",
    "repo_edit",
    "verification",
    "local_commit",
    "exact_sha_push_grant",
    "simulated_push",
    "remote_observation",
    "separate_draft_pr_authorization",
    "exact_sha_ci",
    "review_receipt",
    "STOP",
]


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


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(
            content=response_text,
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    return _chat


class _PaidPlanner:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, *, goal, failure_context=None, **kw):
        self.calls.append(goal.id)
        raise RuntimeError("test: paid planner would have been called")


def _goal_task(db, owner_id, **spec_kwargs):
    goal = planner.create_goal(
        db, owner_id=owner_id, title="execution-complete", original_instruction="local", created_by="founder"
    )
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "repo_edit")
    spec_kwargs.setdefault("target_files", ["backend/tests/test_scratch_edit.py"])
    planner.create_plan(db, goal=goal, rationale="complete", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def test_fetch_origin_base_sha_timeout_is_typed_publication_timeout(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise WorktreeTimeoutError("timed out")

    monkeypatch.setattr("app.mainai_execution.worktree._run_git", _boom)
    with pytest.raises(PublicationTimeoutError):
        fetch_origin_base_sha(tmp_path, BASE_SHA)


def test_fetch_origin_base_sha_git_failure_is_typed_network_error(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise WorktreeError("git fetch failed (128): Could not resolve host")

    monkeypatch.setattr("app.mainai_execution.worktree._run_git", _boom)
    with pytest.raises(PublicationNetworkError):
        fetch_origin_base_sha(tmp_path, BASE_SHA)


def test_fetch_origin_base_sha_mismatch_is_approved_base_moved(tmp_path, monkeypatch):
    class _Result:
        stdout = "cccccccccccccccccccccccccccccccccccccccc\n"

    def _run(args, **kwargs):
        if args[:1] == ["rev-parse"]:
            return _Result()
        return _Result()

    monkeypatch.setattr("app.mainai_execution.worktree._run_git", _run)
    with pytest.raises(ApprovedBaseMovedError):
        fetch_origin_base_sha(tmp_path, BASE_SHA)


def test_worktree_base_fetch_failure_classifies_and_suppresses_paid_replan(db_session, owner_id):
    _, task = _goal_task(db_session, owner_id)
    cause = PublicationNetworkError("WorktreeBaseFetchFailed")
    evidence = cause.evidence()
    assert classify_publication_failure(cause, evidence=evidence) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert should_suppress_paid_replan(task, evidence=evidence, cause=cause) is True


@pytest.mark.asyncio
async def test_create_task_worktree_fetch_timeout_never_calls_paid_planner(
    db_session, owner_id, monkeypatch, tmp_path
):
    from app.config import get_settings
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    async def _fake_ref(self, branch: str) -> str:
        return BASE_SHA

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_ref)
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(tmp_path / "missing.git"))

    real_run = worktree_module._run_git

    def _run(args, *, cwd, timeout=60, input_text=None):
        if args and args[0] == "fetch":
            raise WorktreeTimeoutError("base fetch timed out")
        return real_run(args, cwd=cwd, timeout=timeout, input_text=input_text)

    monkeypatch.setattr(worktree_module, "_run_git", _run)

    goal, task = _goal_task(db_session, owner_id)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    with pytest.raises(PublicationTimeoutError):
        await create_task_worktree(
            db_session, task=task, job=job, lease_generation=1, executor_id="worker-1"
        )


@pytest.mark.asyncio
async def test_repo_edit_base_fetch_failure_is_stop_not_paid_replan(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    from app.config import get_settings
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    async def _fake_ref(self, branch: str) -> str:
        return BASE_SHA

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_ref)
    monkeypatch.setattr(worktree_module, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(tmp_path / "missing.git"))

    real_run = worktree_module._run_git

    def _run(args, *, cwd, timeout=60, input_text=None):
        if args and args[0] == "fetch":
            raise WorktreeError("git fetch failed (128): Could not resolve host github.com")
        return real_run(args, cwd=cwd, timeout=timeout, input_text=input_text)

    monkeypatch.setattr(worktree_module, "_run_git", _run)

    paid = _PaidPlanner()
    monkeypatch.setattr(replan_mod, "propose_plan_via_ai", paid)

    goal, task = _goal_task(
        db_session,
        owner_id,
        verification_plan=[{"kind": "targeted_tests", "target": "tests/test_scratch_edit.py"}],
    )
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    generation = claim_next_mainai_job(superuser_db, "worker-1", 120)[2]
    await run_task_execution_job(
        db_session,
        job.id,
        owner_id,
        worker_id="worker-1",
        lease_generation=generation,
        lease_seconds=120,
    )
    db_session.commit()
    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.failed
    assert task.next_retry_at is None
    assert find_replan_trigger(db_session, goal=goal) is None
    with pytest.raises(ReplanSuppressedError):
        await trigger_replan(db_session, goal=goal, failed_task=task, dispatched_by="test")
    assert paid.calls == []


def test_first_local_committed_salvage_still_allowed_after_one_dead_attempt():
    assert (
        _classify(
            {
                "worktree_current_commit": LOCAL_COMMIT,
                "worktree_local_head_sha": LOCAL_COMMIT,
                "worktree_base_sha": BASE_SHA,
                "remote_branch_exists": False,
                "has_finalized_checkpoint": False,
            }
        )
        == RecoveryClassification.local_committed_not_pushed
    )


@pytest.mark.asyncio
async def test_complete_first_cycle_rehearsal_stops_before_merge(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat(
            '{"files": [{"path": "backend/tests/test_scratch_edit.py", '
            '"content": "def test_scratch_edit():\\n    assert 2 + 3 == 5\\n"}], '
            '"commit_message": "Document isolated first-cycle edit"}'
        ),
    )
    dry = await run_founder_supervised_local_dry_run(
        db_session, owner_id=owner_id, claim_db=superuser_db, repo_root=tmp_path / "ops"
    )
    repo = seed_isolated_repo(tmp_path / "harness")
    published = run_rehearsal(repo)

    assert dry["stages"] == [
        "goal",
        "clear_state",
        "proposed_task",
        "approval_required",
        "founder_approval",
        "dispatch",
        "local_verification",
        "STOP",
    ]
    assert published.stages == HARNESS_STAGES
    assert published.pr is not None
    assert published.pr["draft"] is True
    assert published.ci_passed is True
    assert published.review_ok is True
    assert published.stopped is True
    assert published.publication_ok is True
    assert published.remote_sha == published.local_sha
    assert published.detail["pr_grant_distinct"] is True
    assert dry["stopped"] is True
    assert dry["merged"] is False
    assert dry["go"] is False
    final = dry["final_snapshot"]
    assert final["first_cycle_state"] == STATE_STOP_BEFORE_MERGE
    assert final["go"] is False
    assert "merge" not in final["authorized_actions"]
    goal_row = superuser_db.execute(
        sa_text("SELECT status FROM mainai_goals WHERE id = :id"),
        {"id": dry["goal_id"]},
    ).one()
    assert goal_row[0] == MainAIGoalStatus.blocked.value
    conclude(
        "FC-COMPLETE rehearsal Goal→authorization→edit→verify→publication sim→draft PR→exact-SHA CI→review→STOP",
        PASS,
        boundary="execution.complete_rehearsal",
        detail=(
            f"dry_stages={dry['stages']}; harness_stages={published.stages}; "
            f"sha={published.local_sha}; draft={published.pr['draft']}; "
            f"ci={published.ci_passed}; review={published.review_ok}; merged={dry['merged']}"
        ),
        production_module="app.mainai_execution.first_cycle_ops",
    )


def test_unresolved_security_failures_stay_visible():
    go_nogo = json.loads(
        (Path(__file__).resolve().parents[3] / "docs" / "first_cycle_staging_go_nogo.json").read_text()
    )
    remaining = " ".join(go_nogo["remaining_codex"])
    for token in ("CRED-1/2/3", "E2E-3", "GRANT-PROD", "TA-12", "CI-6", "#280"):
        assert token in remaining
    assert go_nogo["go"] is False
    conclude(
        "FC-COMPLETE unresolved Codex security rows remain FAIL/BLOCKED",
        PASS,
        boundary="docs.first_cycle_staging_go_nogo",
        detail=remaining,
        production_module=None,
    )


@REMAINING_OUT_OF_SCOPE
def test_production_grant_transport_still_not_implemented():
    conclude(
        "GRANT-PROD production grant interface",
        FAIL,
        boundary="production.grants",
        detail="Codex owns production grants/transport; this candidate does not implement them.",
        production_module="app.mainai_execution.execution_job",
    )


def test_staging_pins_this_completion_successor():
    root = Path(__file__).resolve().parents[3]
    go_nogo = json.loads((root / "docs" / "first_cycle_staging_go_nogo.json").read_text())
    manifest = json.loads((root / "docs" / "first_cycle_staging_manifest.json").read_text())
    assert go_nogo["branch"] == THIS_BRANCH
    assert manifest["branch"] == THIS_BRANCH
    assert go_nogo["parent_289"] == FROZEN_289
    assert go_nogo["parent_290"] == PARENT_290
    assert manifest["successor_of"]["pr"] == 290
    assert manifest["successor_of"]["sha"] == PARENT_290
    assert go_nogo["pr"] == 291
    assert manifest["pr"] == 291
    assert go_nogo["go"] is False
    assert manifest["go"] is False
    assert "certified Path A" not in str(manifest["inputs"]["pr_276"].get("role") or "")
