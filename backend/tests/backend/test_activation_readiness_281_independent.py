"""Independent examiner probes for frozen activation-readiness #281 @ abc88b4.

Evidence-only. Does not modify production code. Re-exercises the #277 P1 gaps
(composition ancestry is checked in the exam document; these tests cover the
event-bound CI wait, workflow identity, SHA case, and activation-base claims).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.integrations import github_client as github_client_mod
from app.mainai_execution.ci_wait import evaluate_required_check_runs, poll_ci_wait, suite_bindings
from app.mainai_execution.required_checks import (
    ACTIVATION_REQUIRED_CHECKS,
    CI_STAGE_BRANCH_PUSHED,
    CI_STAGE_PR_OPEN,
    CI_WORKFLOW_PATH,
    GITHUB_ACTIONS_APP_ID,
    GITHUB_ACTIONS_APP_SLUG,
    RequiredCheckContractError,
    expected_events,
)
from app.mainai_execution.worktree import git_changed_paths_no_renames, path_exists_in_git_revision
from app.models.mainai_recovery import ACTIVATION_INTEGRATION_BRANCH, is_protected_branch

SHA = "a" * 40
NAMES = [c.name for c in ACTIVATION_REQUIRED_CHECKS]
HEAD = "claude/mainai-task-00000000-0000-4000-8000-000000000000"
BASE = ACTIVATION_INTEGRATION_BRANCH
PUSH, PR, OTHER_WF = 1, 2, 3
CI = CI_WORKFLOW_PATH
SUITES = {
    PUSH: {"event": "push", "path": CI},
    PR: {"event": "pull_request", "path": CI},
    OTHER_WF: {"event": "push", "path": ".github/workflows/other.yml"},
}
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(name, *, suite, sha=SHA, conclusion="success", status="completed", slug=GITHUB_ACTIONS_APP_SLUG, app_id=GITHUB_ACTIONS_APP_ID, run_id=1):
    return {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": slug, "id": app_id},
        "check_suite": {"id": suite},
    }


def _green(suites=(PUSH, PR)):
    out = []
    n = 1
    for suite in suites:
        for name in NAMES:
            out.append(_run(name, suite=suite, run_id=n))
            n += 1
    return out


def _eval(runs, *, events=("push", "pull_request"), sha=SHA, suites=SUITES):
    return evaluate_required_check_runs(
        runs, sha=sha, required_checks=ACTIVATION_REQUIRED_CHECKS, required_events=events, suites=suites
    )


def test_indep_277_p1_one_suite_alone_no_longer_passes():
    """Former #277 P1: push-green with no PR suite used to PASS. Must stay pending."""
    done, passed, ev = _eval(_green(suites=(PUSH,)))
    assert (done, passed) == (False, False)
    assert all(row["state"] == "missing" for row in ev["required"] if row["event"] == "pull_request")
    assert all(row["state"] == "passed" for row in ev["required"] if row["event"] == "push")


def test_indep_both_suites_green_pass():
    assert _eval(_green())[:2] == (True, True)


def test_indep_wrong_app_never_substitutes():
    runs = [r for r in _green() if r["name"] != NAMES[1]]
    runs.append(_run(NAMES[1], suite=PUSH, slug="vercel", app_id=8329, run_id=99))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(s.get("app_slug") == "vercel" for s in ev["rejected_runs"])


def test_indep_skipped_required_check_fails():
    runs = _green()
    runs.append(_run(NAMES[0], suite=PUSH, conclusion="skipped", run_id=10_000))
    assert _eval(runs)[:2] == (True, False)


def test_indep_vercel_extra_does_not_fail_green_set():
    runs = _green() + [_run("Vercel Preview Comments", suite=99, slug="vercel", app_id=8329, conclusion="failure", run_id=500)]
    assert _eval(runs)[:2] == (True, True)


def test_indep_stale_sha_ignored():
    assert _eval([_run(n, suite=PUSH, sha="b" * 40, run_id=i + 1) for i, n in enumerate(NAMES)])[:2] == (False, False)


def test_indep_second_suite_failure_fails():
    runs = _green(suites=(PUSH,)) + [_run(n, suite=PR, conclusion="failure" if n == NAMES[0] else "success", run_id=1000 + i) for i, n in enumerate(NAMES)]
    assert _eval(runs)[:2] == (True, False)


def test_indep_sha_match_is_case_insensitive():
    runs = [_run(n, suite=s, sha=SHA.upper(), run_id=i + 1 + 100 * s) for s in (PUSH, PR) for i, n in enumerate(NAMES)]
    assert _eval(runs, sha=SHA)[:2] == (True, True)


def test_indep_same_name_from_another_workflow_never_counts():
    runs = [r for r in _green() if not (r["name"] == NAMES[2] and r["check_suite"]["id"] == PUSH)]
    runs.append(_run(NAMES[2], suite=OTHER_WF, run_id=77))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(r["reason"] == "workflow_path" for r in ev["rejected_runs"])


def test_indep_unbound_suite_never_counts():
    runs = [r for r in _green() if not (r["name"] == NAMES[2] and r["check_suite"]["id"] == PR)]
    runs.append(_run(NAMES[2], suite=99, run_id=88))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(r["reason"] == "unbound_suite" for r in ev["rejected_runs"])


def test_indep_pr_open_into_activation_base_requires_both_events():
    assert expected_events(stage=CI_STAGE_PR_OPEN, head_branch=HEAD, base_branch=BASE) == ("push", "pull_request")


def test_indep_branch_pushed_does_not_require_pr_suite():
    assert expected_events(stage=CI_STAGE_BRANCH_PUSHED, head_branch=HEAD, base_branch=None) == ("push",)


def test_indep_unsatisfiable_contract_is_an_error():
    try:
        expected_events(stage=CI_STAGE_PR_OPEN, head_branch="feature/x", base_branch="release/x")
        raise AssertionError("expected RequiredCheckContractError")
    except RequiredCheckContractError:
        pass


def test_indep_suite_bindings_ignore_other_sha():
    wr = [
        {"check_suite_id": PUSH, "event": "push", "path": CI, "head_sha": SHA.upper()},
        {"check_suite_id": PR, "event": "pull_request", "path": CI, "head_sha": "b" * 40},
    ]
    assert suite_bindings(wr, sha=SHA) == {PUSH: {"event": "push", "path": CI}}


def test_indep_poll_ci_wait_does_not_reread_pr_identity():
    """Codex remainder: PR head/base identity re-read during wait is still absent."""
    source = inspect.getsource(poll_ci_wait)
    assert "get_pull_request" not in source
    assert "baseRefOid" not in source
    assert "list_check_runs" in source
    assert "list_workflow_runs_for_sha" in source


def test_indep_github_client_has_no_merge_method_and_paginates_workflow_runs():
    src = Path(github_client_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GitHubClient"
        for node in node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "merge" not in methods and "merge_pull_request" not in methods
    assert "list_check_runs" in methods
    assert "list_workflow_runs_for_sha" in methods
    assert "create_pull_request" in methods
    assert "_get_paginated_key" in methods
    assert "_MAX_PAGES = 50" in src
    assert "per_page=100" in src


def test_indep_path_a_ta10_helpers_still_present():
    assert callable(git_changed_paths_no_renames)
    assert callable(path_exists_in_git_revision)
    src = inspect.getsource(git_changed_paths_no_renames)
    assert "--no-renames" in src


def test_indep_first_cycle_probe_file_absent_and_integration_branch_protected():
    probe = REPO_ROOT / "backend/tests/backend/test_first_cycle_probe.py"
    assert not probe.exists()
    assert not is_protected_branch("backend/tests/backend/test_first_cycle_probe.py")
    assert is_protected_branch(ACTIVATION_INTEGRATION_BRANCH)
    assert is_protected_branch("mainai/anything")
    assert not is_protected_branch("claude/mainai-task-x")


def test_indep_ci_yml_covers_mainai_and_read_token():
    text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"mainai/**"' in text
    assert "permissions:\n  contents: read" in text
    assert "pull_request_target" not in text


def test_indep_required_set_is_twelve_github_actions_success_only():
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12
    assert all(
        c.app_slug == "github-actions"
        and c.app_id == 15368
        and c.allowed_conclusions == ("success",)
        and c.workflow_path == CI
        for c in ACTIVATION_REQUIRED_CHECKS
    )
    assert any(c.name == "Frontend — npm audit" for c in ACTIVATION_REQUIRED_CHECKS)
    assert all("Vercel" not in c.name for c in ACTIVATION_REQUIRED_CHECKS)


def test_indep_demo_1_fixture_declares_target_files():
    text = (REPO_ROOT / "backend/tests/backend/test_mainai_execution_demos.py").read_text(encoding="utf-8")
    assert '"target_files": ["backend/tests/test_demo_doc_fix.py"]' in text
