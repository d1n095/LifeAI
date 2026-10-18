"""Independent examiner probes for frozen activation-readiness #277 @ f0c1d70.

These tests do not modify production code. They re-exercise Claude's exact-set wait and
activation-base claims, then add cases her suite does not cover. Characterization tests
that document a remaining gap are labeled INDEP-GAP-*.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.integrations import github_client as github_client_mod
from app.mainai_execution.ci_wait import evaluate_required_check_runs, poll_ci_wait
from app.mainai_execution.required_checks import (
    ACTIVATION_REQUIRED_CHECKS,
    GITHUB_ACTIONS_APP_ID,
    GITHUB_ACTIONS_APP_SLUG,
    RequiredCheck,
)
from app.mainai_execution.worktree import (
    git_changed_paths_no_renames,
    path_exists_in_git_revision,
)
from app.models.mainai_recovery import ACTIVATION_INTEGRATION_BRANCH, is_protected_branch

SHA = "a" * 40
NAMES = [c.name for c in ACTIVATION_REQUIRED_CHECKS]
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(
    name,
    *,
    sha=SHA,
    conclusion="success",
    status="completed",
    slug=GITHUB_ACTIONS_APP_SLUG,
    app_id=GITHUB_ACTIONS_APP_ID,
    suite=1,
    run_id=1,
    workflow=None,
):
    payload = {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": slug, "id": app_id},
        "check_suite": {"id": suite},
        "started_at": "2026-10-17T00:00:00Z",
    }
    if workflow is not None:
        payload["html_url"] = f"https://github.com/d1n095/LifeAI/actions/workflows/{workflow}"
    return payload


def _green(suite=1):
    return [_run(n, suite=suite, run_id=i + 1) for i, n in enumerate(NAMES)]


def _eval(runs, required=ACTIVATION_REQUIRED_CHECKS, sha=SHA):
    return evaluate_required_check_runs(runs, sha=sha, required_checks=required)


# ---------------------------------------------------------------- reproduced HOLDS
def test_indep_missing_required_check_stays_pending():
    runs = [r for r in _green() if r["name"] != NAMES[0]]
    assert _eval(runs)[:2] == (False, False)


def test_indep_wrong_app_never_substitutes():
    runs = [r for r in _green() if r["name"] != NAMES[1]]
    runs.append(_run(NAMES[1], slug="vercel", app_id=8329, run_id=99))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(s["app_slug"] == "vercel" for s in ev["rejected_substitutions"])


def test_indep_skipped_required_check_fails():
    runs = _green()
    runs[0] = _run(NAMES[0], conclusion="skipped", run_id=1)
    assert _eval(runs)[:2] == (True, False)


def test_indep_vercel_extra_does_not_fail_green_set():
    runs = _green() + [_run("Vercel Preview Comments", slug="vercel", app_id=8329, conclusion="failure", run_id=500)]
    assert _eval(runs)[:2] == (True, True)


def test_indep_stale_sha_ignored():
    assert _eval([_run(n, sha="b" * 40, run_id=i + 1) for i, n in enumerate(NAMES)])[:2] == (False, False)


def test_indep_second_suite_failure_fails():
    runs = _green(suite=1) + [_run(NAMES[0], suite=2, conclusion="failure", run_id=1000)]
    assert _eval(runs)[:2] == (True, False)


# ---------------------------------------------------------------- gaps / extra adversarial
def test_indep_gap_one_suite_alone_is_enough_to_pass():
    """Docstring claims push and pull_request suites are each required. Implementation
    passes as soon as every required name has at least one completed success, even if
    a second GitHub Actions suite has not created runs yet (late PR CI)."""
    done, passed, ev = _eval(_green(suite=1))
    assert (done, passed) == (True, True)
    assert all(len(row["runs"]) == 1 for row in ev["required"])


def test_indep_string_app_id_does_not_match_int_contract():
    runs = [r for r in _green() if r["name"] != NAMES[0]]
    runs.append(_run(NAMES[0], app_id="15368", run_id=77))  # JSON-ish string, not int
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert next(r["state"] for r in ev["required"] if r["name"] == NAMES[0]) == "missing"


def test_indep_completed_with_null_conclusion_fails():
    runs = _green()
    runs[0] = _run(NAMES[0], conclusion=None, status="completed", run_id=1)
    assert _eval(runs)[:2] == (True, False)


def test_indep_sha_match_is_case_sensitive():
    runs = [_run(n, sha="A" * 40, run_id=i + 1) for i, n in enumerate(NAMES)]
    done, passed, ev = _eval(runs, sha="a" * 40)
    assert (done, passed) == (False, False)
    assert ev["stale_sha_runs_ignored"] == 12


def test_indep_matching_ignores_workflow_field():
    """Contract stores workflow='CI' but evaluate_required_check_runs never reads it.
    A same-name github-actions run from another workflow would count."""
    runs = [_run(n, workflow="evil.yml", run_id=i + 1) for i, n in enumerate(NAMES)]
    assert _eval(runs)[:2] == (True, True)


def test_indep_poll_ci_wait_does_not_reread_pr_identity():
    source = inspect.getsource(poll_ci_wait)
    assert "get_pull_request" not in source
    assert "baseRefOid" not in source
    assert "list_check_runs" in source


def test_indep_github_client_has_no_merge_method():
    names = [n for n, _ in inspect.getmembers(github_client_mod.GitHubClient, predicate=inspect.isfunction)]
    assert "merge" not in names
    assert "merge_pull_request" not in names
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
    assert "create_pull_request" in methods


def test_indep_path_a_ta10_helpers_still_present():
    assert callable(git_changed_paths_no_renames)
    assert callable(path_exists_in_git_revision)
    src = inspect.getsource(git_changed_paths_no_renames)
    assert "--no-renames" in src


def test_indep_first_cycle_probe_file_absent_and_not_protected():
    probe = REPO_ROOT / "backend/tests/backend/test_first_cycle_probe.py"
    assert not probe.exists()
    assert not is_protected_branch("backend/tests/backend/test_first_cycle_probe.py")
    assert is_protected_branch(ACTIVATION_INTEGRATION_BRANCH)
    assert is_protected_branch("mainai/anything")


def test_indep_ci_yml_covers_mainai_and_read_token():
    text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"mainai/**"' in text
    assert "permissions:\n  contents: read" in text
    assert "pull_request_target" not in text


def test_indep_required_set_is_twelve_github_actions_success_only():
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12
    assert all(
        c.app_slug == "github-actions" and c.app_id == 15368 and c.allowed_conclusions == ("success",)
        for c in ACTIVATION_REQUIRED_CHECKS
    )
    assert any(c.name == "Frontend — npm audit" for c in ACTIVATION_REQUIRED_CHECKS)
    assert all("Vercel" not in c.name for c in ACTIVATION_REQUIRED_CHECKS)
