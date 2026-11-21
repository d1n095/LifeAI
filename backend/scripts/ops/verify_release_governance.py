#!/usr/bin/env python3
"""Read-only verification of the GitHub prerequisites for a protected release branch.

Nothing here writes. It answers one question an administrator can act on: *what exactly is missing
before `main` can be a protected release branch, and are the checks I would require real?*

Why a script instead of a checklist: the repository currently has no `main`, a Claude branch as its
default, no branch protection and no rulesets. A required status check whose context name does not
match a real job is accepted by the API and then never runs — the branch is protected against
nothing while looking protected. This proves the context names against live workflow data first.

Every fact is read from the API; documentation is not evidence. The commands that would apply the
protection are printed, never executed: repository-wide settings are the founder's decision, and
changing them from here could also disrupt other agents working on the default branch.

Usage:
    python backend/scripts/ops/verify_release_governance.py [--repo OWNER/REPO] [--out report.json]

Exit codes: 0 every prerequisite verified, 2 at least one prerequisite is missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPO = "d1n095/LifeAI"

# The two contexts GITHUB_ENFORCEMENT_ACTIONS.md names. Both are checked against real workflow
# data below; a typo here would silently protect nothing.
REQUIRED_CHECKS = ("Security", "All required checks passed")


def _gh(args: list[str]) -> tuple[bool, object]:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip().splitlines()[:2]
    text = result.stdout.strip()
    if not text:
        return True, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        pass
    # `--jq` without an enclosing array prints one JSON document per line (JSONL). Parsing that as
    # a list is what made the first run report "workflows not readable" for a perfectly readable
    # workflow list.
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        return True, [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return True, text


def check_default_branch(repo: str) -> tuple[bool, dict]:
    ok, data = _gh(["repo", "view", repo, "--json", "defaultBranchRef,visibility"])
    if not ok or not isinstance(data, dict):
        return False, {"error": "repository not readable", "detail": data}
    branch = (data.get("defaultBranchRef") or {}).get("name")
    return True, {"default_branch": branch, "visibility": data.get("visibility")}


def check_main_branch(repo: str) -> tuple[bool, dict]:
    ok, data = _gh(["api", f"repos/{repo}/branches/main"])
    if ok and isinstance(data, dict) and data.get("name") == "main":
        return True, {"exists": True, "head": (data.get("commit") or {}).get("sha")}
    return False, {"exists": False, "probe": "GET /repos/{repo}/branches/main"}


def check_protection(repo: str) -> tuple[bool, dict]:
    ok, data = _gh(["api", f"repos/{repo}/branches/main/protection"])
    if ok and isinstance(data, dict):
        checks = data.get("required_status_checks") or {}
        return True, {
            "present": True,
            "strict": checks.get("strict"),
            "contexts": checks.get("contexts"),
            "enforce_admins": (data.get("enforce_admins") or {}).get("enabled"),
            "required_approvals": (data.get("required_pull_request_reviews") or {}).get(
                "required_approving_review_count"
            ),
            "code_owner_reviews": (data.get("required_pull_request_reviews") or {}).get(
                "require_code_owner_reviews"
            ),
        }
    return False, {"present": False, "probe": data}


def check_rulesets(repo: str) -> tuple[bool, dict]:
    ok, data = _gh(["api", f"repos/{repo}/rulesets"])
    if ok and isinstance(data, list):
        return True, {"count": len(data), "names": [r.get("name") for r in data if isinstance(r, dict)]}
    return False, {"count": None, "probe": data}


def check_codeowners(repo: str, ref: str | None) -> tuple[bool, dict]:
    """Resolve CODEOWNERS at the commit that would become `main`, not on the current default.

    The current default branch is an unrelated Claude branch that does not carry the file. Asking
    without a ref produced a false "missing prerequisite" for a file that is committed in the
    release candidate.
    """
    url = f"repos/{repo}/contents/.github/CODEOWNERS" + (f"?ref={ref}" if ref else "")
    ok, data = _gh(["api", url])
    if ok and isinstance(data, dict) and data.get("path"):
        return True, {"path": data["path"], "bytes": data.get("size"), "ref": ref or "default branch"}
    return False, {"path": None, "ref": ref, "probe": data}


def _local_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def check_required_contexts_exist(repo: str, sha: str | None = None) -> tuple[bool, dict]:
    """The part a checklist gets wrong: prove each required context is a job that really runs.

    Scoped to the commit under test (`head_sha`) and to a run that has a verdict. Asking for the
    newest run in the repository meant an unrelated branch's push — still building — became the
    evidence, and a real context looked missing because its jobs had not been created yet.
    """
    ok, workflows = _gh(
        ["api", f"repos/{repo}/actions/workflows", "--jq", "[.workflows[] | {id, name, path}]"]
    )
    if not ok or not isinstance(workflows, list):
        return False, {"error": "workflows not readable", "detail": workflows}
    names = {w.get("name") for w in workflows if isinstance(w, dict)}

    # Ask the CI workflow for its own latest run. The repository-wide "most recent run" was an
    # unrelated skipped workflow, so the gate job was never in the job list and a real required
    # context looked missing.
    ci = next((w for w in workflows if isinstance(w, dict) and w.get("name") == "CI"), None)
    query = "?per_page=20" + (f"&head_sha={sha}" if sha else "")
    ok_runs, runs = _gh(
        [
            "api",
            f"repos/{repo}/actions/workflows/{ci.get('id') if ci else 'ci.yml'}/runs{query}",
            "--jq",
            "[.workflow_runs[] | {id, name, conclusion, status, head_sha, head_branch, created_at}]",
        ]
    )
    considered = runs if isinstance(runs, list) else []
    completed = [r for r in considered if isinstance(r, dict) and r.get("conclusion")]
    run = completed[0] if completed else (considered[0] if considered else {})

    job_names: list[str] = []
    if run.get("id"):
        ok_jobs, jobs = _gh(
            ["api", f"repos/{repo}/actions/runs/{run['id']}/jobs", "--jq", "[.jobs[] | .name]"]
        )
        if ok_jobs and isinstance(jobs, list):
            job_names = jobs

    verified = {}
    for context in REQUIRED_CHECKS:
        # 'Security' is a workflow name; 'All required checks passed' is a job name inside CI.
        verified[context] = context in names or context in job_names
    detail = {
        "workflow_names": sorted(n for n in names if n),
        "run_used": run or None,
        "runs_considered": len(considered),
        "completed_runs": len(completed),
        "contexts_verified": verified,
    }
    if considered and not completed:
        # Say precisely why this is unproven instead of implying a context does not exist.
        detail["pending"] = "no completed CI run for this commit yet"
    return bool(completed) and all(verified.values()), detail


def check_security_features(repo: str) -> tuple[bool, dict]:
    ok, data = _gh(["api", f"repos/{repo}"])
    if not ok or not isinstance(data, dict):
        return False, {"error": "repository metadata not readable"}
    security = data.get("security_and_analysis") or {}
    return True, {
        "secret_scanning": (security.get("secret_scanning") or {}).get("status"),
        "push_protection": (security.get("secret_scanning_push_protection") or {}).get("status"),
        "dependabot_security_updates": (security.get("dependabot_security_updates") or {}).get("status"),
        "advanced_security": (security.get("advanced_security") or {}).get("status"),
    }


def build_commands(repo: str) -> list[str]:
    contexts = " ".join(f"-f 'required_status_checks.contexts[]={c}'" for c in REQUIRED_CHECKS)
    return [
        "# 1. create the release branch at the reviewed baseline (does not change the default)",
        f"gh api -X POST repos/{repo}/git/refs -f ref=refs/heads/main -f sha=<REVIEWED_BASELINE_SHA>",
        "# 2. protect it (strict, no admin bypass, one founder review, CODEOWNERS)",
        f"gh api -X PUT repos/{repo}/branches/main/protection "
        f"-F required_status_checks.strict=true {contexts} -F enforce_admins=true "
        f"-F required_pull_request_reviews.required_approving_review_count=1 "
        f"-F required_pull_request_reviews.dismiss_stale_reviews=true "
        f"-F required_pull_request_reviews.require_code_owner_reviews=true -F restrictions=",
        "# 3. only after the protection is verified: switch the default and retarget open PRs",
        f"gh api -X PATCH repos/{repo} -f default_branch=main",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--ref",
        default=None,
        help="commit that would become the release branch (default: this repository's HEAD)",
    )
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    ref = args.ref or _local_head()

    results = {}
    for name, fn in (
        ("repository", check_default_branch),
        ("branch.main", check_main_branch),
        ("protection.main", check_protection),
        ("rulesets", check_rulesets),
        ("codeowners", lambda repo: check_codeowners(repo, ref)),
        ("required_contexts", lambda repo: check_required_contexts_exist(repo, ref)),
        ("security_features", check_security_features),
    ):
        ok, detail = fn(args.repo)
        results[name] = {"ok": ok, "detail": detail}

    blocking = [name for name, r in results.items() if not r["ok"]]
    report = {
        "tool": "lifeai-release-governance-verify",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": args.repo,
        "checked_ref": ref,
        "mutations_performed": False,
        "checks": results,
        "ready_to_apply": not blocking,
        "blocking_prerequisites": blocking,
        "commands_to_apply_after_authorization": build_commands(args.repo),
        "note": "Nothing here changed a repository setting. Applying these commands is the "
        "founder's decision and should happen when no other agent is mid-work on the current "
        "default branch.",
    }

    for name, r in results.items():
        print(f"[{'ok  ' if r['ok'] else 'miss'}] {name}")
        print(f"       {json.dumps(r['detail'], ensure_ascii=False)[:220]}")
    print()
    print(f"ready_to_apply: {report['ready_to_apply']}")
    if blocking:
        print(f"blocking: {', '.join(blocking)}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report: {args.out}")
    return 0 if report["ready_to_apply"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
