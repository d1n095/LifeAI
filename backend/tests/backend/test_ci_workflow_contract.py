"""File-level contract on `.github/workflows/ci.yml` for the activation architecture.

- Trigger matrix: `push` and `pull_request` cover main, claude/**, cursor/**, codex/** AND
  mainai/** (activation integration branch: creation, post-merge, and PRs targeting it), with no
  path filters or ignore lists that could silently skip required checks.
- Least privilege: top-level `permissions: contents: read`; no job widens it.
- Required-check contract: every entry of ACTIVATION_REQUIRED_CHECKS is a real, always-run
  job in this workflow (exact display name, no `if:`), and every individual required job is
  gated by the aggregate `all-checks-passed`. Renaming or conditioning a required job fails here.

This is a static contract. The live proof is GitHub running the workflow on the exact SHA.
"""

import fnmatch
import re
from pathlib import Path

import yaml

from app.mainai_execution.required_checks import ACTIVATION_REQUIRED_CHECKS, CI_WORKFLOW_NAME
from app.models.mainai_recovery import ACTIVATION_INTEGRATION_BRANCH

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_BRANCH_FILTERS = ["main", "claude/**", "cursor/**", "codex/**", "mainai/**"]


def _workflow() -> dict:
    data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    if "on" not in data and True in data:
        data["on"] = data.pop(True)
    return data


def _github_branch_filter_matches(pattern: str, ref: str) -> bool:
    """GitHub branch-filter glob: `**` matches across '/', `*` does not."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, ref) is not None


def _runs(event: str, branch: str) -> bool:
    filters = _workflow()["on"][event]["branches"]
    return any(_github_branch_filter_matches(p, branch) for p in filters)


def test_workflow_name_matches_contract():
    assert _workflow()["name"] == CI_WORKFLOW_NAME


def test_push_and_pull_request_branch_filters_are_exact():
    on = _workflow()["on"]
    assert set(on) == {"push", "pull_request"}
    for event in ("push", "pull_request"):
        assert on[event]["branches"] == EXPECTED_BRANCH_FILTERS
        for forbidden in ("paths", "paths-ignore", "branches-ignore", "tags", "tags-ignore"):
            assert forbidden not in on[event], f"{event}.{forbidden} could silently skip required checks"
    assert "pull_request_target" not in on


def test_trigger_matrix():
    task_branch = "claude/mainai-task-6f1c2a9e-0000-4000-8000-000000000000"
    cases = [
        # (event, branch-or-base, expected) -- push: pushed ref; pull_request: PR BASE branch
        ("push", ACTIVATION_INTEGRATION_BRANCH, True),        # activation-branch creation / post-merge CI
        ("pull_request", ACTIVATION_INTEGRATION_BRANCH, True),  # PR CI for PRs targeting it
        ("push", task_branch, True),                          # MainAI task-branch CI
        ("push", "mainai/release-founder-alpha", True),
        ("push", "main", True),
        ("pull_request", "main", True),
        ("push", "claude/det-kommer-mer-879lcm", True),
        ("push", "cursor/mainai-execution-path-a-ta8b-ta10-91c1", True),
        ("pull_request", "cursor/mainai-execution-path-a-task-authority-91c1", True),
        ("push", "codex/frontend-npm-audit-fc4-remediation", True),
        ("pull_request", "codex/founder-alpha-final-composed-candidate", True),
        ("push", "examiner/activation-readiness-package-2026-10-17", False),
        ("push", "feature/anything", False),
        ("push", "mainai", False),
        ("push", "claude", False),
    ]
    results = {(e, b): _runs(e, b) for e, b, _ in cases}
    assert results == {(e, b): exp for e, b, exp in cases}


def test_glob_helper_matches_github_semantics():
    assert _github_branch_filter_matches("mainai/**", "mainai/integration-founder-supervised-v1")
    assert _github_branch_filter_matches("mainai/**", "mainai/a/b/c")
    assert not _github_branch_filter_matches("mainai/*", "mainai/a/b")
    assert not _github_branch_filter_matches("mainai/**", "mainaix/a")
    assert fnmatch.fnmatch("x", "x")


def test_top_level_permissions_are_read_only_and_no_job_widens_them():
    wf = _workflow()
    assert wf.get("permissions") == {"contents": "read"}
    for job_id, job in wf["jobs"].items():
        perms = job.get("permissions")
        if perms is None:
            continue
        assert isinstance(perms, dict), f"{job_id}: permissions must be an explicit map"
        assert all(v in ("read", "none") for v in perms.values()), f"{job_id} widens GITHUB_TOKEN: {perms}"


def _job_display_names(job_id: str, job: dict) -> list[str]:
    name = job.get("name", job_id)
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if "${{ matrix.mode }}" in name:
        return [name.replace("${{ matrix.mode }}", mode) for mode in matrix.get("mode", [])]
    return [name]


def test_every_required_check_is_an_always_run_job_in_this_workflow():
    jobs = _workflow()["jobs"]
    by_display: dict[str, str] = {}
    for job_id, job in jobs.items():
        for display in _job_display_names(job_id, job):
            assert display not in by_display, f"duplicate job display name {display!r}"
            by_display[display] = job_id
    for check in ACTIVATION_REQUIRED_CHECKS:
        assert check.name in by_display, f"required check {check.name!r} is not a job name in ci.yml"
        job_id = by_display[check.name]
        assert job_id == check.job, f"{check.name!r} is job {job_id!r}, contract says {check.job!r}"
        job = jobs[job_id]
        if job_id == "all-checks-passed":
            assert job.get("if") == "always()"
        else:
            assert "if" not in job, f"required job {job_id!r} is conditional and may be skipped"


def test_aggregate_gate_needs_every_individual_required_job():
    jobs = _workflow()["jobs"]
    needs = set(jobs["all-checks-passed"]["needs"])
    individual = {c.job for c in ACTIVATION_REQUIRED_CHECKS if c.job != "all-checks-passed"}
    assert individual <= needs
    step = "\n".join(s.get("run", "") for s in jobs["all-checks-passed"]["steps"])
    assert "failure" in step and "cancelled" in step


def test_branch_gated_and_deploy_jobs_are_not_required():
    jobs = _workflow()["jobs"]
    required_jobs = {c.job for c in ACTIVATION_REQUIRED_CHECKS}
    for job_id, job in jobs.items():
        if "if" in job and job_id != "all-checks-passed":
            assert job_id not in required_jobs, f"conditional job {job_id!r} must not be a required check"
