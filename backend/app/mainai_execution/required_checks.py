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
from dataclasses import dataclass

GITHUB_ACTIONS_APP_SLUG = "github-actions"
GITHUB_ACTIONS_APP_ID = 15368
CI_WORKFLOW_NAME = "CI"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
REQUIRED_CHECKS_CONTRACT = "mainai.activation.required_checks/v1"
# The only conclusion that satisfies a required check unless the contract explicitly allows more.
DEFAULT_ALLOWED_CONCLUSIONS: tuple[str, ...] = ("success",)


class RequiredCheckContractError(ValueError):
    """A required-check set is empty, malformed, or ambiguous. Fail closed."""


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    app_slug: str = GITHUB_ACTIONS_APP_SLUG
    app_id: int | None = GITHUB_ACTIONS_APP_ID
    workflow: str | None = CI_WORKFLOW_NAME
    job: str | None = None
    allowed_conclusions: tuple[str, ...] = DEFAULT_ALLOWED_CONCLUSIONS

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "app_slug": self.app_slug,
            "app_id": self.app_id,
            "workflow": self.workflow,
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
    return RequiredCheck(
        name=name,
        app_slug=app_slug,
        app_id=app_id,
        workflow=raw.get("workflow"),
        job=raw.get("job"),
        allowed_conclusions=tuple(allowed),
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


def required_checks_payload(checks=ACTIVATION_REQUIRED_CHECKS) -> dict:
    """Immutable payload stored on a CI wait's condition (and digestible for attestations)."""
    normalized = normalize_required_checks(checks)
    entries = [c.as_dict() for c in normalized]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "contract": REQUIRED_CHECKS_CONTRACT,
        "required_checks": entries,
        "required_checks_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def required_checks_from_condition(condition: dict | None) -> tuple[RequiredCheck, ...]:
    """Reads and re-validates the set stored on a wait. A tampered digest fails closed."""
    condition = condition or {}
    entries = condition.get("required_checks")
    normalized = normalize_required_checks(entries)
    expected = required_checks_payload(normalized)["required_checks_sha256"]
    stored = condition.get("required_checks_sha256")
    if stored != expected:
        raise RequiredCheckContractError("stored required-check digest does not match the stored set -- refusing to evaluate")
    return normalized
