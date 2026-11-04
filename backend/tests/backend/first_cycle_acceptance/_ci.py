"""Independent exact-set CI/review oracle. Success-only. Never treats skipped/neutral as PASS.

This is the first-cycle acceptance bar. Production `evaluate_check_runs` on frozen #276 is
weaker (skipped/neutral pass, no exact set). Those gaps must FAIL, not be patched here.
"""

from __future__ import annotations

from dataclasses import dataclass

GITHUB_ACTIONS_APP_SLUG = "github-actions"
GITHUB_ACTIONS_APP_ID = 15368
DEFAULT_ALLOWED = ("success",)
MAX_CHECK_RUNS_EVALUATED = 30

ACTIVATION_REQUIRED_CHECKS: tuple[dict, ...] = (
    {"name": "Frontend — TypeScript & ESLint", "job": "lint-and-typecheck"},
    {"name": "Frontend — npm audit", "job": "npm-audit"},
    {"name": "Frontend — build (docker)", "job": "frontend-build"},
    {"name": "Frontend — build (vercel)", "job": "frontend-build"},
    {"name": "Backend — unit/integration tests", "job": "backend-tests"},
    {"name": "Backend — RLS & session-security tests", "job": "rls-security-tests"},
    {"name": "Backend — account lifecycle & rate-limit tests", "job": "account-rate-limit-tests"},
    {"name": "Backend — Alembic migration check", "job": "migration-check"},
    {"name": "E2E — Playwright (full stack)", "job": "e2e-tests"},
    {"name": "E2E — Library / Founder Knowledge Studio", "job": "e2e-library-studio-tests"},
    {"name": "E2E — same-origin proxy", "job": "same-origin-proxy-test"},
    {"name": "All required checks passed", "job": "all-checks-passed"},
)


def _check(name: str, job: str) -> dict:
    return {
        "name": name,
        "job": job,
        "app_slug": GITHUB_ACTIONS_APP_SLUG,
        "app_id": GITHUB_ACTIONS_APP_ID,
        "allowed_conclusions": list(DEFAULT_ALLOWED),
    }


REQUIRED = tuple(_check(item["name"], item["job"]) for item in ACTIVATION_REQUIRED_CHECKS)


def make_run(
    *,
    name: str,
    sha: str,
    conclusion: str = "success",
    status: str = "completed",
    app_slug: str = GITHUB_ACTIONS_APP_SLUG,
    app_id: int = GITHUB_ACTIONS_APP_ID,
    suite_id: int = 1,
    run_id: int = 1,
    started_at: str = "2026-10-18T00:00:00Z",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "app": {"slug": app_slug, "id": app_id},
        "check_suite": {"id": suite_id},
    }


def exact_set_runs(sha: str, *, suite_ids: tuple[int, ...] = (11, 22), conclusion: str = "success") -> list[dict]:
    """Push suite + pull_request suite for every required check."""
    runs = []
    run_id = 1
    for suite_id in suite_ids:
        for check in REQUIRED:
            runs.append(
                make_run(
                    name=check["name"],
                    sha=sha,
                    conclusion=conclusion,
                    suite_id=suite_id,
                    run_id=run_id,
                )
            )
            run_id += 1
    return runs


def _app_slug(run: dict) -> str | None:
    app = run.get("app") if isinstance(run.get("app"), dict) else {}
    slug = app.get("slug")
    return slug if isinstance(slug, str) else None


def _app_id(run: dict) -> int | None:
    app = run.get("app") if isinstance(run.get("app"), dict) else {}
    app_id = app.get("id")
    return app_id if isinstance(app_id, int) and not isinstance(app_id, bool) else None


def _suite_key(run: dict):
    suite = run.get("check_suite") if isinstance(run.get("check_suite"), dict) else {}
    return suite.get("id")


def _order_key(run: dict):
    run_id = run.get("id")
    return (run_id if isinstance(run_id, int) else -1, str(run.get("started_at") or ""))


def _matches(run: dict, required: dict) -> bool:
    if run.get("name") != required["name"] or _app_slug(run) != required["app_slug"]:
        return False
    return required["app_id"] is None or _app_id(run) == required["app_id"]


@dataclass
class CIVerdict:
    done: bool
    passed: bool
    evidence: dict


def evaluate_exact_set(check_runs: list[dict], *, sha: str, required: tuple[dict, ...] = REQUIRED) -> CIVerdict:
    if not sha:
        return CIVerdict(True, False, {"contract_error": "wait has no sha"})
    if not required:
        return CIVerdict(True, False, {"contract_error": "empty required-check set"})
    if len(check_runs or []) > MAX_CHECK_RUNS_EVALUATED:
        # Still evaluate, but record the overflow. Overflow alone is not a PASS.
        overflow = True
    else:
        overflow = False

    on_sha, stale = [], []
    for run in check_runs or []:
        if not isinstance(run, dict):
            continue
        (on_sha if run.get("head_sha") == sha else stale).append(run)

    per_check = []
    substitutions = []
    claimed = set()
    pending = False
    failed = False
    for check in required:
        matching = [r for r in on_sha if _matches(r, check)]
        for r in on_sha:
            if r.get("name") == check["name"] and r not in matching:
                substitutions.append({"name": r.get("name"), "app_slug": _app_slug(r), "app_id": _app_id(r)})
        latest_by_suite = {}
        for r in matching:
            key = _suite_key(r)
            if key not in latest_by_suite or _order_key(r) > _order_key(latest_by_suite[key]):
                latest_by_suite[key] = r
        counted = list(latest_by_suite.values())
        for r in matching:
            claimed.add(id(r))
        if len(counted) < 2:
            # First cycle requires both push and pull_request suites.
            state = "missing_suite" if counted else "missing"
            pending = True
        elif any(r.get("status") != "completed" for r in counted):
            state = "in_progress"
            pending = True
        elif all(r.get("conclusion") in check["allowed_conclusions"] for r in counted):
            state = "passed"
        else:
            state = "failed"
            failed = True
        per_check.append({"name": check["name"], "state": state, "runs": len(counted)})

    extras = [
        {"name": r.get("name"), "app_slug": _app_slug(r), "conclusion": r.get("conclusion")}
        for r in on_sha
        if id(r) not in claimed
    ]
    evidence = {
        "sha": sha,
        "required": per_check,
        "overflow": overflow,
        "check_run_count": len(check_runs or []),
        "unexpected_checks_evidence_only": extras,
        "rejected_substitutions": substitutions,
        "stale_sha_runs_ignored": len(stale),
    }
    if pending:
        return CIVerdict(False, False, evidence)
    return CIVerdict(True, not failed, evidence)


@dataclass
class ReviewReceipt:
    pr_number: int
    head_sha: str
    reviewer_identity: str
    builder_identity: str
    state: str  # approved | changes_requested | commented

    def ok(self) -> bool:
        if self.reviewer_identity == self.builder_identity:
            return False
        return self.state == "approved"


def evaluate_review(*, receipt: ReviewReceipt | None, pr_head_sha: str, builder_identity: str) -> tuple[bool, str]:
    if receipt is None:
        return False, "no review receipt; first cycle must STOP"
    if receipt.head_sha != pr_head_sha:
        return False, "review receipt SHA does not match current PR head"
    if receipt.builder_identity != builder_identity:
        return False, "receipt builder identity does not match the builder"
    if receipt.reviewer_identity == receipt.builder_identity:
        return False, "reviewer and builder identities must be distinct"
    if receipt.state != "approved":
        return False, f"negative or non-approving review: {receipt.state}"
    return True, "approved"
