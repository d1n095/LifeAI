"""Read-only GitHub development intake into the durable coordination ledger.

Connects observed PRs, exact head SHAs, required CI, commit ancestry and review
evidence to `DurableCoordinationService`. It does not publish, merge, grant Founder
Authority, or claim that Cursor/Claude/Codex received a message.

Credential-free public reads are used when no token is configured. Unavailable
auth, rate limits and API failures are recorded as BLOCKED with a named capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.agent_coordination.durable import FIRST_CYCLE_GO, DurableCoordinationService
from app.mainai_execution.ci_wait import evaluate_required_check_runs, suite_bindings
from app.mainai_execution.required_checks import (
    ACTIVATION_REQUIRED_CHECKS,
    CI_EVENT_PULL_REQUEST,
    CI_EVENT_PUSH,
    CI_STAGE_PR_OPEN,
    expected_events,
)

API_BASE = "https://api.github.com"
DEFAULT_PUBLIC_REPO = "d1n095/LifeAI"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
GITHUB_ACTIONS_APP_SLUG = "github-actions"
GITHUB_ACTIONS_APP_ID = 15368
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"

MISSING_RATE_LIMIT = "github_read_rate_limit"
MISSING_UNAVAILABLE = "github_read_unavailable"
MISSING_FORBIDDEN = "github_read_permission"
MISSING_MESSAGING = "agent_messaging_adapter"
MISSING_PUBLICATION = "codex_github_publication_authority"


class GitHubReadError(RuntimeError):
    def __init__(self, kind: str, *, status_code: int | None = None, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.status_code = status_code

    @property
    def missing_capability(self) -> str:
        if self.kind == "rate_limited":
            return MISSING_RATE_LIMIT
        if self.kind == "forbidden":
            return MISSING_FORBIDDEN
        return MISSING_UNAVAILABLE


def _iso(value: str | None = None) -> str:
    if value:
        return value
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _norm_sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    sha = value.strip().lower()
    return sha if _GIT_SHA.match(sha) else None


def builder_identity_for_branch(branch: str) -> str:
    name = (branch or "").split("/", 1)[0].lower()
    if name in {"cursor", "claude", "codex"}:
        return name
    return "cursor"


def builder_instruction(pr_number: int, sha: str | None = None) -> str:
    return f"github-pr-{int(pr_number)}-build"


def examiner_instruction(pr_number: int, sha: str | None = None) -> str:
    return f"github-pr-{int(pr_number)}-examine"


@dataclass
class GitHubReadClient:
    """GET-only GitHub client. Public unauthenticated access is the default."""

    repo: str = DEFAULT_PUBLIC_REPO
    token: str | None = None
    transport: Callable[..., httpx.Response] | None = None
    timeout: float = 20.0

    @property
    def access_mode(self) -> str:
        return "token" if self.token else "public_unauthenticated"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "LifeAI-MainAI-github-readonly-intake",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str) -> Any:
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        if self.transport is not None:
            response = self.transport("GET", url, headers=self._headers())
        else:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, headers=self._headers())
        remaining = response.headers.get("X-RateLimit-Remaining")
        if response.status_code == 429 or (response.status_code == 403 and remaining == "0"):
            raise GitHubReadError("rate_limited", status_code=response.status_code, message=response.text[:300])
        if response.status_code in {401, 403}:
            raise GitHubReadError("forbidden", status_code=response.status_code, message=response.text[:300])
        if response.status_code >= 500:
            raise GitHubReadError("unavailable", status_code=response.status_code, message=response.text[:300])
        if response.status_code >= 400:
            raise GitHubReadError("unavailable", status_code=response.status_code, message=response.text[:300])
        return response.json() if response.content else {}

    def discover_pull_request(self, number: int) -> dict[str, Any]:
        pr = self.get(f"/repos/{self.repo}/pulls/{int(number)}")
        sha = _norm_sha((pr.get("head") or {}).get("sha"))
        commits = self.get(f"/repos/{self.repo}/pulls/{int(number)}/commits?per_page=100")
        reviews = self.get(f"/repos/{self.repo}/pulls/{int(number)}/reviews?per_page=100")
        check_runs = []
        workflow_runs = []
        if sha:
            encoded = quote(sha, safe="")
            checks = self.get(f"/repos/{self.repo}/commits/{encoded}/check-runs?per_page=100")
            check_runs = list(checks.get("check_runs") or []) if isinstance(checks, dict) else []
            runs = self.get(f"/repos/{self.repo}/actions/runs?head_sha={encoded}&per_page=100")
            workflow_runs = list(runs.get("workflow_runs") or []) if isinstance(runs, dict) else []
        ancestry: list[str] = []
        for commit in commits if isinstance(commits, list) else []:
            commit_sha = _norm_sha(commit.get("sha"))
            if commit_sha:
                ancestry.append(commit_sha)
            for parent in commit.get("parents") or []:
                parent_sha = _norm_sha(parent.get("sha") if isinstance(parent, dict) else None)
                if parent_sha:
                    ancestry.append(parent_sha)
        return {
            "number": int(pr.get("number") or number),
            "title": pr.get("title"),
            "draft": bool(pr.get("draft")),
            "merged": bool(pr.get("merged")),
            "state": pr.get("state") or "open",
            "html_url": pr.get("html_url"),
            "head_sha": sha,
            "base_sha": _norm_sha((pr.get("base") or {}).get("sha")),
            "head_branch": (pr.get("head") or {}).get("ref"),
            "base_branch": (pr.get("base") or {}).get("ref"),
            "updated_at": pr.get("updated_at"),
            "ancestry": sorted(set(ancestry)),
            "reviews": reviews if isinstance(reviews, list) else [],
            "check_runs": check_runs,
            "workflow_runs": workflow_runs,
        }


def classify_required_ci(
    *,
    sha: str,
    head_branch: str,
    base_branch: str,
    check_runs: list[dict[str, Any]],
    workflow_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    events = expected_events(stage=CI_STAGE_PR_OPEN, head_branch=head_branch, base_branch=base_branch)
    suites = suite_bindings(workflow_runs, sha=sha)
    done, passed, evidence = evaluate_required_check_runs(
        check_runs,
        sha=sha,
        required_checks=ACTIVATION_REQUIRED_CHECKS,
        required_events=events,
        suites=suites,
    )
    freeze_blockers: list[str] = []
    if not done:
        freeze_blockers.append("required_checks_incomplete")
    if done and not passed:
        freeze_blockers.append("required_checks_failed")
    return {
        "sha": sha,
        "done": done,
        "passed": passed,
        "freeze_allowed": bool(done and passed),
        "freeze_blockers": freeze_blockers,
        "required_events": list(events),
        "evidence": evidence,
    }


def empty_github_intake_state(repo: str | None = None) -> dict[str, Any]:
    return {
        "repo": repo,
        "access": "unknown",
        "missing_capability": None,
        "write_attempted": False,
        "delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "candidates": {},
        "seen_deliveries": [],
        "stale_rejected": [],
    }


class GitHubDevelopmentIntake:
    """Ingests GitHub-shaped observations into an existing durable ledger."""

    def __init__(self, service: DurableCoordinationService | None = None, *, owner_id: str, repo: str = DEFAULT_PUBLIC_REPO):
        self.service = service or DurableCoordinationService()
        self.owner_id = owner_id
        if not getattr(self.service, "_github_intake", None):
            self.service._github_intake = empty_github_intake_state(repo)
        self.service._github_intake["repo"] = repo

    @property
    def state(self) -> dict[str, Any]:
        return self.service._github_intake

    def record_access(self, *, access: str, missing_capability: str | None = None) -> None:
        self.state["access"] = access
        self.state["missing_capability"] = missing_capability

    def block_access(self, error: GitHubReadError) -> dict[str, Any]:
        self.record_access(access="blocked", missing_capability=error.missing_capability)
        return {
            "outcome": "blocked",
            "missing_capability": error.missing_capability,
            "status_code": error.status_code,
            "delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def claim_agent_delivery(self, *, agent_identity: str) -> dict[str, Any]:
        self.state["delivery_claimed"] = False
        return {
            "outcome": "blocked",
            "missing_capability": MISSING_MESSAGING,
            "reason": "no authorized messaging adapter confirmed delivery",
            "agent_identity": agent_identity,
            "delivery_claimed": False,
        }

    def claim_github_publication(self) -> dict[str, Any]:
        return {
            "outcome": "blocked",
            "missing_capability": MISSING_PUBLICATION,
            "reason": "GitHub publication remains Codex-owned",
            "write_attempted": False,
        }

    def ingest_pull_request(self, observation: dict[str, Any], *, actor: str = "mainai") -> dict[str, Any]:
        sha = _norm_sha(observation.get("head_sha"))
        if sha is None:
            return {"outcome": "forged_rejected", "reason": "head_sha is not a git object"}
        number = int(observation["number"])
        head_branch = str(observation.get("head_branch") or "")
        base_branch = str(observation.get("base_branch") or "main")
        ancestry = [_norm_sha(item) for item in observation.get("ancestry") or []]
        ancestry_set = {item for item in ancestry if item}
        ci = classify_required_ci(
            sha=sha,
            head_branch=head_branch,
            base_branch=base_branch,
            check_runs=list(observation.get("check_runs") or []),
            workflow_runs=list(observation.get("workflow_runs") or []),
        )
        reviewable = bool(
            observation.get("state", "open") == "open"
            and not observation.get("draft")
            and not observation.get("merged")
        )
        key = str(number)
        previous = self.state["candidates"].get(key)
        previous_sha = previous.get("head_sha") if previous else None
        builder_id, _builder_dup = self._ensure_obligation(
            agent_identity=builder_identity_for_branch(head_branch),
            instruction=builder_instruction(number, sha),
            workstream="github-builder",
            actor=actor,
        )
        examiner_id = None
        examiner_dup = False
        if reviewable:
            examiner_id, examiner_dup = self._ensure_obligation(
                agent_identity="examiner",
                instruction=examiner_instruction(number, sha),
                workstream="github-examiner",
                actor=actor,
            )
        self.service.record_execution_progress(
            assignment_id=builder_id,
            agent_identity=builder_identity_for_branch(head_branch),
            evidence_kind="test_run",
            evidence={
                "sha": sha,
                "previous_sha": previous_sha,
                "superseded_commit": previous_sha if previous_sha and previous_sha != sha else None,
                "freeze_allowed": ci["freeze_allowed"],
                "freeze_blockers": ci["freeze_blockers"],
                "pr": number,
            },
            event_id=f"ci-{number}-{sha}-{ci['freeze_allowed']}",
        )
        reviews = [
            {
                "id": row.get("id"),
                "state": row.get("state"),
                "commit_id": _norm_sha(row.get("commit_id")),
                "author": ((row.get("user") or {}).get("login") if isinstance(row, dict) else None),
            }
            for row in observation.get("reviews") or []
            if isinstance(row, dict)
        ]
        candidate = {
            "pr": number,
            "title": observation.get("title"),
            "html_url": observation.get("html_url"),
            "head_sha": sha,
            "base_sha": _norm_sha(observation.get("base_sha")),
            "head_branch": head_branch,
            "base_branch": base_branch,
            "draft": bool(observation.get("draft")),
            "merged": bool(observation.get("merged")),
            "reviewable": reviewable,
            "ancestry": sorted(ancestry_set),
            "ci": {k: ci[k] for k in ("done", "passed", "freeze_allowed", "freeze_blockers", "required_events")},
            "reviews": reviews,
            "builder_assignment_id": builder_id,
            "examiner_assignment_id": examiner_id,
            "updated_at": observation.get("updated_at") or _iso(),
            "first_cycle_go": FIRST_CYCLE_GO,
        }
        self.state["candidates"][key] = candidate
        self.record_access(access=self.state.get("access") or "public_unauthenticated")
        return {
            "outcome": "applied",
            "pr": number,
            "head_sha": sha,
            "reviewable": reviewable,
            "freeze_allowed": ci["freeze_allowed"],
            "freeze_blockers": ci["freeze_blockers"],
            "builder_assignment_id": builder_id,
            "examiner_assignment_id": examiner_id,
            "duplicate_examiner": examiner_dup,
            "delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def ingest_event(self, event: dict[str, Any]) -> dict[str, Any]:
        delivery_id = str(event.get("delivery_id") or event.get("id") or "")
        if not delivery_id:
            return {"outcome": "forged_rejected", "reason": "missing delivery_id"}
        if delivery_id in self.state["seen_deliveries"]:
            return {
                "outcome": "replay_ignored",
                "delivery_id": delivery_id,
                "reason": "duplicate webhook delivery",
            }
        self.state["seen_deliveries"].append(delivery_id)
        sha = _norm_sha(event.get("sha") or event.get("head_sha"))
        if sha is None:
            return {"outcome": "forged_rejected", "reason": "event sha is not a git object", "delivery_id": delivery_id}
        pr_number = event.get("pr") or event.get("number")
        if pr_number is None:
            return {"outcome": "stale_rejected", "reason": "event is not bound to a PR", "delivery_id": delivery_id}
        candidate = self.state["candidates"].get(str(int(pr_number)))
        if candidate is None:
            return {"outcome": "buffered", "reason": "no current candidate for PR", "delivery_id": delivery_id, "pr": int(pr_number)}
        current = candidate["head_sha"]
        if sha != current:
            kind = "obsolete_sha" if sha in set(candidate.get("ancestry") or []) else "foreign_sha"
            row = {
                "delivery_id": delivery_id,
                "sha": sha,
                "current_sha": current,
                "pr": int(pr_number),
                "kind": kind,
                "claimed_conclusion": event.get("conclusion"),
            }
            self.state["stale_rejected"].append(row)
            return {
                "outcome": "stale_rejected",
                "reason": "event sha is not the current verified head",
                "kind": kind,
                "freeze_allowed": candidate["ci"]["freeze_allowed"],
                "current_sha": current,
                "delivery_id": delivery_id,
            }
        if event.get("forged") or event.get("app_slug") not in {None, GITHUB_ACTIONS_APP_SLUG}:
            if event.get("app_slug") and event.get("app_slug") != GITHUB_ACTIONS_APP_SLUG:
                return {"outcome": "forged_rejected", "reason": "required-check impersonation", "delivery_id": delivery_id}
        if not event.get("check_runs"):
            return {
                "outcome": "applied",
                "delivery_id": delivery_id,
                "head_sha": current,
                "freeze_allowed": candidate["ci"]["freeze_allowed"],
                "reason": "event acknowledged against current head; no new check runs",
            }
        observation = {
            "number": int(pr_number),
            "title": candidate.get("title"),
            "html_url": candidate.get("html_url"),
            "draft": candidate.get("draft"),
            "merged": candidate.get("merged"),
            "state": "open",
            "head_sha": current,
            "base_sha": candidate.get("base_sha"),
            "head_branch": candidate.get("head_branch"),
            "base_branch": candidate.get("base_branch"),
            "ancestry": candidate.get("ancestry") or [],
            "reviews": event.get("reviews")
            or [
                {
                    "id": r.get("id"),
                    "state": r.get("state"),
                    "commit_id": r.get("commit_id"),
                    "user": {"login": r.get("author")},
                }
                for r in candidate.get("reviews") or []
            ],
            "check_runs": event.get("check_runs") or [],
            "workflow_runs": event.get("workflow_runs") or [],
            "updated_at": event.get("occurred_at"),
        }
        applied = self.ingest_pull_request(observation)
        applied["delivery_id"] = delivery_id
        applied["outcome"] = "applied"
        return applied

    def refresh(self, client: GitHubReadClient, *, numbers: list[int] | None = None) -> dict[str, Any]:
        self.record_access(access=client.access_mode)
        try:
            if numbers:
                observations = [client.discover_pull_request(n) for n in numbers]
            else:
                pulls = client.get(f"/repos/{client.repo}/pulls?state=open&per_page=20")
                observations = []
                for row in pulls if isinstance(pulls, list) else []:
                    observations.append(client.discover_pull_request(int(row["number"])))
        except GitHubReadError as exc:
            return self.block_access(exc)
        results = [self.ingest_pull_request(item) for item in observations]
        return {
            "outcome": "applied",
            "access": client.access_mode,
            "missing_capability": None,
            "results": results,
            "delivery_claimed": False,
            "write_attempted": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def view(self) -> dict[str, Any]:
        candidates = list(self.state["candidates"].values())
        return {
            "capability": "github_readonly_intake",
            "repo": self.state.get("repo"),
            "access": self.state.get("access"),
            "missing_capability": self.state.get("missing_capability"),
            "write_attempted": False,
            "delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
            "blocks_first_cycle": False,
            "candidates": candidates,
            "stale_rejected": list(self.state.get("stale_rejected") or []),
            "seen_deliveries": list(self.state.get("seen_deliveries") or []),
            "needed_review": [
                {
                    "pr": row["pr"],
                    "sha": row["head_sha"],
                    "examiner_assignment_id": row.get("examiner_assignment_id"),
                    "need": "independent_review" if row["ci"]["freeze_allowed"] else "wait_for_required_checks",
                }
                for row in candidates
                if row.get("reviewable")
            ],
        }

    def _ensure_obligation(self, *, agent_identity: str, instruction: str, workstream: str, actor: str) -> tuple[str, bool]:
        result = self.service.assign(
            agent_identity=agent_identity,
            workstream=workstream,
            owner_id=self.owner_id,
            instruction_id=instruction,
            actor=actor,
        )
        assert result.assignment_id is not None
        return result.assignment_id, result.outcome == "idempotent"


def github_check_run(
    *,
    name: str,
    sha: str,
    event: str,
    conclusion: str | None,
    status: str = "completed",
    run_id: int,
    suite_id: int,
    app_slug: str = GITHUB_ACTIONS_APP_SLUG,
    app_id: int = GITHUB_ACTIONS_APP_ID,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-10-22T09:00:00Z",
        "check_suite": {"id": suite_id},
        "app": {"slug": app_slug, "id": app_id},
    }


def github_workflow_run(*, sha: str, event: str, suite_id: int, run_id: int) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "event": event,
        "path": CI_WORKFLOW_PATH,
        "check_suite_id": suite_id,
        "status": "completed",
        "conclusion": "success",
    }


def passing_required_runs(sha: str, *, base_id: int = 1000) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    run_id = base_id
    for event, suite_id in ((CI_EVENT_PUSH, base_id + 1), (CI_EVENT_PULL_REQUEST, base_id + 2)):
        workflows.append(github_workflow_run(sha=sha, event=event, suite_id=suite_id, run_id=run_id))
        run_id += 1
        for check in ACTIVATION_REQUIRED_CHECKS:
            checks.append(
                github_check_run(
                    name=check.name,
                    sha=sha,
                    event=event,
                    conclusion="success",
                    run_id=run_id,
                    suite_id=suite_id,
                )
            )
            run_id += 1
    return checks, workflows
