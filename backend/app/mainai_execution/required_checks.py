"""Repository-side canonical required-check contract for MainAI's CI wait.

MainAI never waits for "every check run that happens to appear". A CI wait is driven only by
an immutable required-check set stored on the wait record when the wait starts. Each entry
binds the exact check-run name to the GitHub App that must produce it, so a same-name check
from another app (Vercel, a different integration, a commit status) can never substitute.

`ACTIVATION_REQUIRED_CHECKS` is the default contract: the 12 always-run jobs of
`.github/workflows/ci.yml` (verified by `tests/backend/test_ci_workflow_contract.py`, which
fails if a job is renamed, made conditional, or dropped from the aggregate gate). It is the
repository's own versioned definition of "CI passed". The activation authority substrate
(execution envelope / preflight receipt) may supply its own Founder-attested set; when it
does, that set is what gets stored on the wait instead of this default.

Branch-gated jobs (combined-container, VPS) and third-party deployment signals (Vercel) are
deliberately excluded: they are skipped or external, so requiring them proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

GITHUB_ACTIONS_APP_SLUG = "github-actions"
GITHUB_ACTIONS_APP_ID = 15368
CI_WORKFLOW_NAME = "CI"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
REQUIRED_CHECKS_CONTRACT = "mainai.activation.required_checks/v1"
# The only conclusion that satisfies a required check unless the contract explicitly allows more.
DEFAULT_ALLOWED_CONCLUSIONS: tuple[str, ...] = ("success",)

# Branch filters of `.github/workflows/ci.yml` (verified byte-for-byte by
# tests/backend/test_ci_workflow_contract.py). They decide which GitHub events can EVER
# produce a CI suite for a given head/base, so a wait never expects a suite that cannot exist.
CI_PUSH_BRANCH_FILTERS: tuple[str, ...] = ("main", "claude/**", "cursor/**", "codex/**", "mainai/**")
CI_PULL_REQUEST_BRANCH_FILTERS: tuple[str, ...] = ("main", "claude/**", "cursor/**", "codex/**", "mainai/**")

# Activation CI stages. `branch_pushed`: the task branch is on GitHub but no PR exists yet, so
# only the `push` suite can exist. `pr_open`: a PR exists, so the `pull_request` suite is also
# required whenever the PR base is covered by the workflow's pull_request filter.
CI_STAGE_BRANCH_PUSHED = "branch_pushed"
CI_STAGE_PR_OPEN = "pr_open"
CI_STAGES = (CI_STAGE_BRANCH_PUSHED, CI_STAGE_PR_OPEN)
CI_EVENT_PUSH = "push"
CI_EVENT_PULL_REQUEST = "pull_request"


class RequiredCheckContractError(ValueError):
    """A required-check set is empty, malformed, or ambiguous. Fail closed."""


def branch_filter_matches(pattern: str, ref: str) -> bool:
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


def expected_events(*, stage: str, head_branch: str | None, base_branch: str | None) -> tuple[str, ...]:
    """The GitHub events whose CI suite MUST be green for this stage. Never expects a suite that
    the workflow triggers cannot produce (no deadlock), never omits one that they will produce
    (no single-suite overclaim). An empty result is a contract error: no CI could ever count."""
    if stage not in CI_STAGES:
        raise RequiredCheckContractError(f"unknown CI stage {stage!r}")
    if not isinstance(head_branch, str) or not head_branch:
        raise RequiredCheckContractError("CI contract requires the pushed head branch")
    events: list[str] = []
    if any(branch_filter_matches(p, head_branch) for p in CI_PUSH_BRANCH_FILTERS):
        events.append(CI_EVENT_PUSH)
    if stage == CI_STAGE_PR_OPEN:
        if not isinstance(base_branch, str) or not base_branch:
            raise RequiredCheckContractError("a PR-stage CI contract requires the PR base branch")
        if any(branch_filter_matches(p, base_branch) for p in CI_PULL_REQUEST_BRANCH_FILTERS):
            events.append(CI_EVENT_PULL_REQUEST)
    if not events:
        raise RequiredCheckContractError(
            f"no CI suite can ever run for head {head_branch!r} / base {base_branch!r}; refusing a wait that can only time out"
        )
    return tuple(events)


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    app_slug: str = GITHUB_ACTIONS_APP_SLUG
    app_id: int | None = GITHUB_ACTIONS_APP_ID
    workflow: str | None = CI_WORKFLOW_NAME
    job: str | None = None
    allowed_conclusions: tuple[str, ...] = DEFAULT_ALLOWED_CONCLUSIONS
    # The workflow file that must have produced the run (bound through the run's check suite).
    workflow_path: str | None = CI_WORKFLOW_PATH

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "app_slug": self.app_slug,
            "app_id": self.app_id,
            "workflow": self.workflow,
            "workflow_path": self.workflow_path,
            "job": self.job,
            "allowed_conclusions": list(self.allowed_conclusions),
        }


def _ci(name: str, job: str) -> RequiredCheck:
    return RequiredCheck(name=name, job=job)


ACTIVATION_REQUIRED_CHECKS: tuple[RequiredCheck, ...] = (
    _ci("Frontend — TypeScript & ESLint", "lint-and-typecheck"),
    _ci("Frontend — npm audit", "npm-audit"),
    _ci("Frontend — build (docker)", "frontend-build"),
    _ci("Frontend — build (vercel)", "frontend-build"),
    _ci("Backend — unit/integration tests", "backend-tests"),
    _ci("Backend — RLS & session-security tests", "rls-security-tests"),
    _ci("Backend — account lifecycle & rate-limit tests", "account-rate-limit-tests"),
    _ci("Backend — Alembic migration check", "migration-check"),
    _ci("E2E — Playwright (full stack)", "e2e-tests"),
    _ci("E2E — Library / Founder Knowledge Studio", "e2e-library-studio-tests"),
    _ci("E2E — same-origin proxy", "same-origin-proxy-test"),
    _ci("All required checks passed", "all-checks-passed"),
)


def required_check_from_dict(raw: dict) -> RequiredCheck:
    if not isinstance(raw, dict):
        raise RequiredCheckContractError(f"required check entry must be an object, got {raw!r}")
    name = raw.get("name")
    app_slug = raw.get("app_slug")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise RequiredCheckContractError(f"required check name must be a non-empty, trimmed string: {name!r}")
    if not isinstance(app_slug, str) or not app_slug:
        raise RequiredCheckContractError(f"required check {name!r} must bind an app_slug")
    app_id = raw.get("app_id")
    if app_id is not None and (not isinstance(app_id, int) or isinstance(app_id, bool)):
        raise RequiredCheckContractError(f"required check {name!r} app_id must be an integer or null")
    allowed = raw.get("allowed_conclusions", list(DEFAULT_ALLOWED_CONCLUSIONS))
    if not isinstance(allowed, (list, tuple)) or not allowed or not all(isinstance(c, str) and c for c in allowed):
        raise RequiredCheckContractError(f"required check {name!r} allowed_conclusions must be a non-empty list")
    workflow_path = raw.get("workflow_path", CI_WORKFLOW_PATH)
    if workflow_path is not None and (not isinstance(workflow_path, str) or not workflow_path):
        raise RequiredCheckContractError(f"required check {name!r} workflow_path must be a non-empty string or null")
    return RequiredCheck(
        name=name,
        app_slug=app_slug,
        app_id=app_id,
        workflow=raw.get("workflow"),
        job=raw.get("job"),
        allowed_conclusions=tuple(allowed),
        workflow_path=workflow_path,
    )


def normalize_required_checks(checks) -> tuple[RequiredCheck, ...]:
    """Validated, de-ambiguated, name-sorted set. Empty or duplicate (name, app) fails closed."""
    items = [c if isinstance(c, RequiredCheck) else required_check_from_dict(c) for c in (checks or [])]
    if not items:
        raise RequiredCheckContractError("required check set is empty -- refusing a vacuous CI pass")
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.name, item.app_slug)
        if key in seen:
            raise RequiredCheckContractError(f"duplicate required check {item.name!r} for app {item.app_slug!r}")
        seen.add(key)
    return tuple(sorted(items, key=lambda c: (c.name, c.app_slug)))


def _digest(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def required_checks_payload(
    checks=ACTIVATION_REQUIRED_CHECKS,
    *,
    stage: str,
    head_branch: str | None,
    base_branch: str | None,
) -> dict:
    """Immutable payload stored on a CI wait's condition (and digestible for attestations).

    Binds the check set, the CI stage, the exact head/base branches and the derived set of
    GitHub events whose suites must each be green. The digest covers all of it."""
    normalized = normalize_required_checks(checks)
    events = expected_events(stage=stage, head_branch=head_branch, base_branch=base_branch)
    body = {
        "contract": REQUIRED_CHECKS_CONTRACT,
        "required_checks": [c.as_dict() for c in normalized],
        "stage": stage,
        "head_branch": head_branch,
        "base_branch": base_branch,
        "required_events": list(events),
    }
    return {**body, "required_checks_sha256": _digest(body)}


def required_checks_from_condition(condition: dict | None) -> tuple[tuple[RequiredCheck, ...], tuple[str, ...]]:
    """Reads and re-validates the contract stored on a wait -> (checks, required_events).
    A missing field, a tampered digest, or events that disagree with the stored stage and
    branches fail closed."""
    condition = condition or {}
    normalized = normalize_required_checks(condition.get("required_checks"))
    events = condition.get("required_events")
    if not isinstance(events, list) or not events or not all(e in (CI_EVENT_PUSH, CI_EVENT_PULL_REQUEST) for e in events):
        raise RequiredCheckContractError("stored contract has no valid required_events")
    recomputed = expected_events(
        stage=condition.get("stage"), head_branch=condition.get("head_branch"), base_branch=condition.get("base_branch")
    )
    if tuple(events) != recomputed:
        raise RequiredCheckContractError("stored required_events disagree with the stored stage/branches")
    body = {k: condition.get(k) for k in ("contract", "stage", "head_branch", "base_branch")}
    body["required_checks"] = [c.as_dict() for c in normalized]
    body["required_events"] = list(events)
    if condition.get("required_checks_sha256") != _digest(body):
        raise RequiredCheckContractError("stored required-check digest does not match the stored contract -- refusing to evaluate")
    return normalized, tuple(events)
