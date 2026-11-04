"""End-to-end simulated first cycle. Network-free. Production gaps stay FAIL/BLOCKED."""

from __future__ import annotations

import inspect

import pytest

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._git import git
from first_cycle_acceptance._grants import GrantError
from first_cycle_acceptance._lifecycle import apply_authorized_edit, run_rehearsal
from first_cycle_acceptance._matrix import BLOCKED, FAIL, PASS
from first_cycle_acceptance.conftest import REMAINING_OUT_OF_SCOPE, conclude

pytestmark = pytest.mark.first_cycle_acceptance

EXPECTED_STAGES = [
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


def test_e2e_harness_rehearsal_reaches_stop(isolated_repo):
    result = run_rehearsal(isolated_repo)
    holds = (
        result.stages == EXPECTED_STAGES
        and result.publication_ok
        and result.remote_sha == result.local_sha
        and result.pr is not None
        and result.pr["draft"] is True
        and result.ci_passed
        and result.review_ok
        and result.stopped
        and result.detail["pr_grant_distinct"]
    )
    conclude(
        "E2E-1 harness rehearsal Goal→STOP",
        PASS if holds else FAIL,
        boundary="harness.lifecycle",
        detail=(
            f"stages={result.stages}; sha={result.local_sha}; remote={result.remote_sha}; "
            f"publication={result.publication_ok} {result.detail['publication']}; "
            f"ci={result.ci_passed}; review={result.review_ok}; pr={result.pr}"
        ),
        production_module=None,
    )
    assert holds


def test_e2e_ungranted_push_is_refused_by_remote_hook(isolated_repo):
    apply_authorized_edit(isolated_repo, branch="claude/mainai-task-ungranted")
    git("add", "-A", cwd=isolated_repo.work)
    git("commit", "-q", "-m", "ungranted", cwd=isolated_repo.work)
    pushed = git(
        "push",
        "origin",
        "claude/mainai-task-ungranted:claude/mainai-task-ungranted",
        cwd=isolated_repo.work,
        check=False,
    )
    refused = pushed.returncode != 0 and "ungranted push refused" in (pushed.stderr or "")
    conclude(
        "E2E-2 ungranted push refused",
        PASS if refused else FAIL,
        boundary="harness.remote_grant_hook",
        detail=f"returncode={pushed.returncode}; stderr={(pushed.stderr or '')[:400]}",
    )
    assert refused
    assert isolated_repo.remote_sha("claude/mainai-task-ungranted") is None


@REMAINING_OUT_OF_SCOPE
def test_e2e_production_push_has_no_grant_gate():
    push = PRODUCTION["worktree"].symbols.get("push_worktree_branch")
    finalize_mod = None
    try:
        from app.mainai_execution import execution_job as execution_job_mod

        finalize_mod = execution_job_mod
        finalize = execution_job_mod._finalize_repo_edit
    except Exception as exc:  # noqa: BLE001
        conclude(
            "E2E-3 production finalize requires exact-SHA push grant",
            BLOCKED,
            boundary="production._finalize_repo_edit",
            detail=f"cannot import: {exc}",
            production_module="app.mainai_execution.execution_job",
        )
        return
    src = inspect.getsource(finalize)
    mentions_grant = (
        "push_grant" in src
        or "require_remote_effect_authority" in src
        or "exact-SHA" in src
        or "exact_sha" in src
    )
    push_src = inspect.getsource(push) if push is not None else ""
    push_gated = "grant" in push_src.lower() and "force" not in push_src.lower().split("grant", 1)[0][-20:]
    production_meets = mentions_grant or bool(PRODUCTION["push_grants"].available)
    # Unconditional push_worktree_branch after path-set check is a FAIL.
    unconditional = "push_worktree_branch" in src and not mentions_grant
    status = FAIL if (unconditional and not production_meets) else (PASS if mentions_grant else BLOCKED)
    conclude(
        "E2E-3 production finalize requires exact-SHA push grant",
        status,
        boundary="production._finalize_repo_edit",
        detail=(
            f"mentions_grant={mentions_grant}; push_gated={push_gated}; "
            f"unconditional_push={unconditional}; push_grants_module={PRODUCTION['push_grants'].available}"
        ),
        production_module="app.mainai_execution.execution_job",
    )


def test_e2e_production_has_no_review_receipt_stop():
    try:
        from app.mainai_execution import execution_job as execution_job_mod
    except Exception as exc:  # noqa: BLE001
        conclude(
            "E2E-4 production STOP after review receipt",
            BLOCKED,
            boundary="production.open_pr",
            detail=f"cannot import: {exc}",
            production_module="app.mainai_execution.execution_job",
        )
        return
    src = inspect.getsource(execution_job_mod)
    has_review = "evaluate_first_cycle_review_receipt" in src
    has_stop = "stop_first_cycle" in src
    conclude(
        "E2E-4 production STOP after review receipt",
        PASS if (has_review and has_stop) else FAIL,
        boundary="production.open_pr.review_receipt",
        detail=f"review_receipt_present={has_review}; stop_first_cycle={has_stop}",
        production_module="app.mainai_execution.execution_job",
    )


def test_e2e_push_and_pr_grants_are_separate(isolated_repo):
    result = run_rehearsal(isolated_repo)
    conclude(
        "E2E-5 separate push and draft-PR grants",
        PASS if result.detail["pr_grant_distinct"] else FAIL,
        boundary="harness.grants.separate_kinds",
        detail=f"distinct={result.detail['pr_grant_distinct']}",
    )


def test_e2e_consumed_push_grant_cannot_open_pr(isolated_repo):
    from first_cycle_acceptance._grants import GrantStore
    from first_cycle_acceptance._lifecycle import SimulatedEffectExecutor, apply_authorized_edit
    from first_cycle_acceptance._git import git, rev_parse

    apply_authorized_edit(isolated_repo, branch="claude/mainai-task-reuse")
    git("add", "-A", cwd=isolated_repo.work)
    git("commit", "-q", "-m", "edit", cwd=isolated_repo.work)
    sha = rev_parse(isolated_repo.work)
    store = GrantStore()
    grant = store.issue(
        kind="push",
        owner_id="o",
        goal_id="g",
        task_id="t",
        job_id="j",
        worktree_id="w",
        repository="test-owner/test-repo",
        branch="claude/mainai-task-reuse",
        base_sha=isolated_repo.base_sha,
        commit_sha=sha,
    )
    executor = SimulatedEffectExecutor(store=store, repo=isolated_repo)
    executor.push(grant, local_sha=sha, branch="claude/mainai-task-reuse")
    with pytest.raises(GrantError):
        store.consume(
            grant.id,
            kind="draft_pr",
            owner_id="o",
            goal_id="g",
            task_id="t",
            job_id="j",
            worktree_id="w",
            repository="test-owner/test-repo",
            branch="claude/mainai-task-reuse",
            base_sha=isolated_repo.base_sha,
            commit_sha=sha,
        )
    conclude(
        "E2E-6 consumed push grant cannot be reused as a PR grant",
        PASS,
        boundary="harness.grants.kind_and_one_use",
        detail="push grant rejected when presented as draft_pr after consume",
    )
