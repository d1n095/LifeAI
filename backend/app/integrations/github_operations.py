"""Read-only, bounded project intelligence over GitHub and existing LifeAI records.

The GitHub payload is an observation, not an instruction source. This module never dispatches
agents or writes to GitHub; recommendations are deterministic rules over explicit evidence.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import all_agents_runtime_snapshot, work_registry_snapshot
from app.capability_reality.service import list_capability_records
from app.config import get_settings
from app.integrations.decision_completeness import assess_decision_completeness
from app.integrations.github_client import GitHubClient, GitHubClientError, GitHubListResult
from app.mainai_executive.why_graph import list_decision_debt, why_feature_exists
from app.models.intelligence_governance import IntelligenceEvidence
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskDependency,
    MainAITaskStatus,
)
from app.models.work_candidate import WorkCandidate

SNAPSHOT_TTL_SECONDS = 60
MAX_DETAILED_PULL_REQUESTS = 8
MAX_WORKFLOW_RUNS_PER_PULL_REQUEST = 2
MAX_COMMENT_LENGTH = 800
_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DEPENDENCY_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:depends\s+on|stacked\s+on|blocked\s+by|requires)"
    r"\s+(?:pull\s+request\s+)?#(\d+)\b"
)
_CANONICAL_PR_CLAIM = re.compile(
    r"(?i)\bcanonical(?:\s+(?:authority|architecture))?\s*[:=]\s*(?:PR\s*)?#(\d+)\b"
)


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return str(value)


def _dependency_numbers(body: str | None) -> list[int]:
    return sorted({int(value) for value in _DEPENDENCY_PATTERN.findall(body or "")})


def _safe_text(value: Any, limit: int = MAX_COMMENT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    return cleaned[:limit]


def _safe_nonnegative_number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    if isinstance(value, str) and len(value) > 64:
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not numeric.is_finite() or numeric < 0:
        return None
    if integer:
        return int(numeric) if numeric == numeric.to_integral_value() else None
    return float(numeric)


def _agent_result_summary(evidence: IntelligenceEvidence) -> dict[str, Any]:
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    return {
        "id": str(evidence.id),
        "assignment_id": evidence.source_ref,
        "evidence_kind": evidence.evidence_kind,
        "review_kind": evidence.review_kind,
        "deterministic": bool(evidence.deterministic),
        "verification_status": "not_independently_verified",
        "observed_at": _iso(evidence.observed_at),
        "tests_passed": payload.get("tests_passed") if isinstance(payload.get("tests_passed"), bool) else None,
        "tests_run": _safe_nonnegative_number(payload.get("tests_run"), integer=True),
        "tests_failed": _safe_nonnegative_number(payload.get("tests_failed"), integer=True),
        "duration_seconds": _safe_nonnegative_number(payload.get("duration_seconds")),
        "cost_tokens": _safe_nonnegative_number(payload.get("cost_tokens"), integer=True),
        "cost_usd": _safe_nonnegative_number(payload.get("cost_usd")),
        "ci_conclusion": _safe_text(payload.get("ci_conclusion"), 80) or None,
        "source": {
            "kind": "intelligence_evidence",
            "id": str(evidence.id),
            "source_ref": evidence.source_ref,
            "href": "/admin/agents",
        },
    }


def _list_items(result: GitHubListResult) -> list[dict]:
    return result.items


def _review_state(reviews: list[dict], *, head_sha: str | None, truncated: bool) -> str:
    if truncated:
        return "unknown_truncated"
    if not head_sha:
        return "unknown_head_sha"
    latest_by_reviewer: dict[str, dict] = {}
    for review in reviews:
        reviewer = review.get("author") or (review.get("user") or {}).get("login")
        if not reviewer:
            continue
        previous = latest_by_reviewer.get(reviewer)
        if previous is None or str(review.get("submitted_at") or "") >= str(previous.get("submitted_at") or ""):
            latest_by_reviewer[reviewer] = review
    submitted = [
        item
        for item in latest_by_reviewer.values()
        if item.get("state") in {"APPROVED", "CHANGES_REQUESTED"}
    ]
    exact_head = [
        item
        for item in submitted
        if str(item.get("commit_id") or item.get("commit_sha") or "").lower() == head_sha.lower()
    ]
    states = {item.get("state") for item in exact_head}
    if "CHANGES_REQUESTED" in states:
        return "changes_requested_exact_head"
    if "APPROVED" in states:
        return "approved_exact_head"
    if any(not (item.get("commit_id") or item.get("commit_sha")) for item in submitted):
        return "approval_sha_unknown"
    if submitted:
        return "approval_stale_head"
    return "pending_or_not_requested"


def _check_state(check: dict) -> str:
    conclusion = str(check.get("conclusion") or "").lower()
    status = str(check.get("status") or "").lower()
    if conclusion in {"failure", "timed_out", "action_required", "startup_failure"}:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion == "skipped":
        return "skipped"
    if conclusion in {"success", "neutral"}:
        return "success"
    if status in {"queued", "in_progress", "pending", "requested", "waiting"}:
        return "pending"
    return "unknown"


def _aggregate_ci_state(details: dict[str, Any]) -> str:
    latest_checks = [
        check for check in details["checks"] if check.get("is_latest_for_check", True)
    ]
    states = [item["state"] for item in latest_checks]
    for run in details["workflow_runs"]:
        if not run.get("is_latest_for_workflow", True):
            continue
        states.append(_check_state(run))
        states.extend(job["state"] for job in run["jobs"])
    if "failed" in states:
        return "failed"
    if "pending" in states:
        return "pending"
    if "cancelled" in states:
        return "cancelled"
    if "skipped" in states:
        return "skipped"
    if not states or "unknown" in states:
        return "unknown"
    if any(
        details.get(key)
        for key in (
            "checks_truncated", "workflow_runs_truncated",
            "workflow_jobs_truncated", "workflow_steps_truncated",
        )
    ):
        return "unknown"
    if any(error in details["detail_errors"] for error in (
        "checks_unavailable",
        "workflow_runs_unavailable",
        "workflow_jobs_unavailable",
        "workflow_run_head_sha_mismatch",
    )):
        return "unknown"
    return "success"


def _mark_latest_checks(checks: list[dict]) -> list[dict]:
    grouped: dict[tuple[Any, Any], list[dict]] = {}
    for item in checks:
        grouped.setdefault((item.get("app_id"), item.get("name")), []).append(item)
    for items in grouped.values():
        if len(items) == 1:
            items[0]["is_latest_for_check"] = True
            continue
        timestamps = [str(item.get("started_at") or "") for item in items]
        latest = max(timestamps)
        if latest:
            for item, timestamp in zip(items, timestamps):
                item["is_latest_for_check"] = timestamp == latest
        else:
            for item in items:
                item["is_latest_for_check"] = False
    return checks


async def _discover_pull_request_details(client: GitHubClient, pull_request: dict) -> dict[str, Any]:
    number = int(pull_request["number"])
    listed_sha = str((pull_request.get("head") or {}).get("sha") or "")
    pull_request_detail = await client.get_pull_request(number)
    detail_head = pull_request_detail.get("head") or {}
    sha = str(detail_head.get("sha") or "")
    valid_sha = bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", sha))
    result: dict[str, Any] = {
        "reviews": [],
        "comments": [],
        "checks": [],
        "workflow_runs": [],
        "workflow_steps_truncated": False,
        "latest_commit": None,
        "head_sha": sha if valid_sha else None,
        "head_branch": detail_head.get("ref"),
        "base_branch": (pull_request_detail.get("base") or {}).get("ref"),
        "base_sha": ((pull_request_detail.get("base") or {}).get("sha")),
        "head_sha_changed_since_list": bool(listed_sha and valid_sha and listed_sha.lower() != sha.lower()),
        "mergeability": {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None},
        "detail_errors": [],
        "detail_observed_at": datetime.now(timezone.utc).isoformat(),
    }

    calls: dict[str, Any] = {
        "reviews": client.list_pull_request_reviews(number, max_pages=5),
        "comments": client.list_pull_request_comments(number),
        "review_comments": client.list_pull_request_review_comments(number),
    }
    if valid_sha:
        calls["checks"] = client.list_check_runs_result(sha)
        calls["workflow_runs"] = client.list_workflow_runs(sha)
        calls["latest_commit"] = client.get_commit_metadata(sha)
    else:
        result["detail_errors"].append("current_head_sha_unavailable")

    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    data = dict(zip(calls, results))
    for name, value in data.items():
        if isinstance(value, asyncio.CancelledError):
            raise value
        if isinstance(value, Exception):
            result["detail_errors"].append(f"{name}_unavailable")
            continue
        if name == "reviews":
            result["reviews"] = [
                {
                    "id": item.get("id"),
                    "author": (item.get("user") or {}).get("login"),
                    "state": item.get("state", "UNKNOWN"),
                    "commit_sha": item.get("commit_id"),
                    "submitted_at": item.get("submitted_at"),
                    "url": item.get("html_url"),
                }
                for item in _list_items(value)
            ]
            result["reviews_truncated"] = value.truncated
        elif name == "comments":
            result["comments"] = [
                {
                    "id": item.get("id"),
                    "kind": "issue_comment",
                    "author": (item.get("user") or {}).get("login"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "body": _safe_text(item.get("body")),
                    "url": item.get("html_url"),
                    "untrusted_content": True,
                }
                for item in _list_items(value)[:20]
            ]
            result["comments_truncated"] = value.truncated or len(value.items) > 20
        elif name == "review_comments":
            result["comments"].extend(
                [
                    {
                        "id": item.get("id"),
                        "kind": "review_comment",
                        "author": (item.get("user") or {}).get("login"),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                        "body": _safe_text(item.get("body")),
                        "url": item.get("html_url"),
                        "untrusted_content": True,
                    }
                    for item in _list_items(value)[:20]
                ]
            )
            result["comments_truncated"] = (
                result.get("comments_truncated", False) or value.truncated or len(value.items) > 20
            )
        elif name == "checks":
            result["checks"] = _mark_latest_checks([
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                    "state": _check_state(item),
                    "app_id": (item.get("app") or {}).get("id"),
                    "app_name": (item.get("app") or {}).get("name"),
                    "head_sha": sha,
                    "url": item.get("html_url") or item.get("details_url"),
                    "started_at": item.get("started_at"),
                    "completed_at": item.get("completed_at"),
                }
                for item in value.items
            ])
            result["checks_truncated"] = value.truncated
        elif name == "workflow_runs":
            runs = sorted(
                _list_items(value),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:MAX_WORKFLOW_RUNS_PER_PULL_REQUEST]
            latest_workflow_keys = set()
            matching_runs = []
            for run in runs:
                if str(run.get("head_sha") or "").lower() == sha.lower():
                    workflow_key = run.get("workflow_id") or run.get("name") or run.get("id")
                    run["is_latest_for_workflow"] = workflow_key not in latest_workflow_keys
                    latest_workflow_keys.add(workflow_key)
                    matching_runs.append(run)
                else:
                    result["detail_errors"].append("workflow_run_head_sha_mismatch")
            result["workflow_jobs_truncated"] = False
            jobs_per_run = await asyncio.gather(
                *(client.list_workflow_jobs(int(run["id"])) for run in matching_runs),
                return_exceptions=True,
            )
            for run, jobs_result in zip(matching_runs, jobs_per_run):
                jobs = []
                if isinstance(jobs_result, asyncio.CancelledError):
                    raise jobs_result
                if isinstance(jobs_result, Exception):
                    result["detail_errors"].append("workflow_jobs_unavailable")
                else:
                    result["workflow_jobs_truncated"] = (
                        result["workflow_jobs_truncated"] or jobs_result.truncated
                    )
                    result["workflow_steps_truncated"] = result["workflow_steps_truncated"] or any(
                        len(job.get("steps", [])) > 64
                        for job in jobs_result.items
                        if isinstance(job.get("steps", []), list)
                    )
                    jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "state": _check_state(job),
                            "head_sha": sha,
                            "url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                            "steps": [
                                {
                                    "name": _safe_text(step.get("name"), 200),
                                    "state": _check_state(step),
                                }
                                for step in (
                                    job.get("steps")
                                    if isinstance(job.get("steps"), list) else []
                                )[:64]
                                if isinstance(step, dict)
                            ],
                        }
                        for job in jobs_result.items
                    ]
                result["workflow_runs"].append(
                    {
                        "id": run.get("id"),
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "url": run.get("html_url"),
                        "head_sha": run.get("head_sha"),
                        "is_latest_for_workflow": run.get("is_latest_for_workflow", False),
                        "created_at": run.get("created_at"),
                        "updated_at": run.get("updated_at"),
                        "jobs": jobs,
                    }
                )
            result["workflow_runs_truncated"] = value.truncated or len(value.items) > len(runs)
        elif name == "latest_commit":
            commit = value.get("commit") or {}
            committer = commit.get("committer") or {}
            result["latest_commit"] = {
                "sha": value.get("sha"),
                "source_head_sha": sha,
                "message": _safe_text(commit.get("message"), 500),
                "committed_at": committer.get("date"),
                "url": value.get("html_url"),
            }
    result["review_state"] = _review_state(
        result["reviews"],
        head_sha=result["head_sha"],
        truncated=result.get("reviews_truncated", False),
    )
    result["ci_state"] = _aggregate_ci_state(result)
    result["mergeability"] = {
        "state": "UNKNOWN" if pull_request_detail.get("mergeable") is None else (
            "MERGEABLE" if pull_request_detail.get("mergeable") else "CONFLICTING"
        ),
        "mergeable": pull_request_detail.get("mergeable"),
        "mergeable_state": pull_request_detail.get("mergeable_state"),
        "observed_at": result["detail_observed_at"],
    }
    try:
        final_detail = await client.get_pull_request(number)
        final_sha = str(((final_detail.get("head") or {}).get("sha")) or "")
    except (GitHubClientError, httpx.RequestError):
        final_sha = ""
        result["detail_errors"].append("head_sha_recheck_unavailable")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", final_sha):
        result["head_sha"] = None
        result["review_state"] = "unknown_head_sha"
        result["checks"] = []
        result["workflow_runs"] = []
        result["latest_commit"] = None
        result["mergeability"] = {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None}
        result["ci_state"] = "unknown"
    elif final_sha.lower() != sha.lower():
        result["head_sha_changed_during_detail"] = True
        result["head_sha"] = final_sha
        result["head_sha_rechecked_at"] = datetime.now(timezone.utc).isoformat()
        result["head_branch"] = (final_detail.get("head") or {}).get("ref")
        result["base_branch"] = (final_detail.get("base") or {}).get("ref")
        result["base_sha"] = (final_detail.get("base") or {}).get("sha")
        result["checks"] = []
        result["workflow_runs"] = []
        result["latest_commit"] = None
        result["mergeability"] = {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None}
        result["review_state"] = "unknown_head_changed"
        result["ci_state"] = "unknown"
        result["detail_errors"].append("head_sha_changed_during_detail")
    else:
        result["head_sha_rechecked_at"] = datetime.now(timezone.utc).isoformat()
    if "reviews_unavailable" in result["detail_errors"] and result["review_state"] not in {
        "unknown_head_sha",
        "unknown_head_changed",
    }:
        result["review_state"] = "unknown_reviews_unavailable"
    elif result["review_state"] not in {"unknown_head_sha", "unknown_head_changed"}:
        result["review_state"] = _review_state(
            result["reviews"],
            head_sha=result["head_sha"],
            truncated=result.get("reviews_truncated", False),
        )
    return result


async def _fetch_github_observation(client: GitHubClient) -> dict[str, Any]:
    repo_result, open_result, closed_result = await asyncio.gather(
        client.get_repository(),
        client.list_pull_requests(state="open"),
        client.list_pull_requests(state="closed", max_pages=2),
    )
    open_prs = sorted(
        open_result.items,
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    closed_prs = closed_result.items
    dependency_state = {int(pr["number"]): "open" for pr in open_prs}
    dependency_state.update(
        {
            int(pr["number"]): "merged" if pr.get("merged_at") else "closed_unmerged"
            for pr in closed_prs
        }
    )

    details_by_number: dict[int, dict[str, Any]] = {}
    detail_prs = open_prs[:MAX_DETAILED_PULL_REQUESTS]
    semaphore = asyncio.Semaphore(4)

    async def bounded_detail(pr: dict) -> tuple[int, dict[str, Any]]:
        async with semaphore:
            try:
                return int(pr["number"]), await _discover_pull_request_details(client, pr)
            except (GitHubClientError, httpx.RequestError, ValueError, KeyError, TypeError):
                return int(pr["number"]), {
                    "reviews": [],
                    "comments": [],
                    "checks": [],
                    "workflow_runs": [],
                    "latest_commit": None,
                    "mergeability": {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None},
                    "review_state": "unknown_not_fetched",
                    "ci_state": "unknown",
                    "head_sha": (pr.get("head") or {}).get("sha"),
                    "head_sha_changed_since_list": False,
                    "detail_errors": ["details_unavailable"],
                    "detail_observed_at": datetime.now(timezone.utc).isoformat(),
                }

    detail_results = await asyncio.gather(*(bounded_detail(pr) for pr in detail_prs))
    details_by_number.update(detail_results)

    pull_requests: list[dict[str, Any]] = []
    for pr in open_prs:
        number = int(pr["number"])
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        detail = details_by_number.get(number)
        dependencies = []
        dependency_numbers = _dependency_numbers(pr.get("body"))
        canonical_claims = sorted({
            int(value) for value in _CANONICAL_PR_CLAIM.findall(_safe_text(pr.get("body"), 20000))
        })
        for dependency_number in dependency_numbers:
            state = dependency_state.get(dependency_number, "unknown")
            dependencies.append(
                {
                    "number": dependency_number,
                    "state": state,
                    "blocked": state != "merged",
                    "url": f"https://github.com/{repo_result.get('full_name')}/pull/{dependency_number}",
                    "source": "explicit_dependency_in_pull_request_body",
                }
            )
        pull_requests.append(
            {
                "number": number,
                "title": _safe_text(pr.get("title"), 500),
                "state": "open",
                "draft": bool(pr.get("draft")),
                "author": (pr.get("user") or {}).get("login"),
                "url": pr.get("html_url"),
                "head_branch": (detail or {}).get("head_branch") or head.get("ref"),
                "head_sha": detail["head_sha"] if detail and "head_sha" in detail else head.get("sha"),
                "head_repository": (head.get("repo") or {}).get("full_name"),
                "base_branch": (detail or {}).get("base_branch") or base.get("ref"),
                "base_sha": (detail or {}).get("base_sha") or base.get("sha"),
                "head_sha_changed_since_list": detail.get("head_sha_changed_since_list", False) if detail else False,
                "head_sha_changed_during_detail": detail.get("head_sha_changed_during_detail", False) if detail else False,
                "head_sha_rechecked_at": detail.get("head_sha_rechecked_at") if detail else None,
                "updated_at": pr.get("updated_at"),
                "created_at": pr.get("created_at"),
                "dependencies": dependencies,
                "canonical_claims": canonical_claims,
                "dependency_evidence": {
                    "state": "explicit_dependencies_found" if dependency_numbers else "not_declared_or_unknown",
                    "method": "explicit_pull_request_body",
                    "source": pr.get("html_url"),
                },
                "mergeability": (
                    detail["mergeability"]
                    if detail
                    else {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None}
                ),
                "review_state": detail["review_state"] if detail else "unknown_not_fetched",
                "ci_state": detail.get("ci_state", "unknown") if detail else "unknown",
                "requested_reviewers": [
                    (reviewer or {}).get("login") for reviewer in pr.get("requested_reviewers", [])
                ],
                "reviews": detail["reviews"] if detail else [],
                "comments": detail["comments"] if detail else [],
                "checks": detail["checks"] if detail else [],
                "workflow_runs": detail["workflow_runs"] if detail else [],
                "workflow_steps_truncated": (
                    detail.get("workflow_steps_truncated", False) if detail else False
                ),
                "latest_commit": detail["latest_commit"] if detail else None,
                "detail_errors": detail["detail_errors"] if detail else ["not_fetched_resource_limit"],
                "details_truncated": (
                    bool(
                        detail.get("reviews_truncated")
                        or detail.get("comments_truncated")
                        or detail.get("checks_truncated")
                        or detail.get("workflow_runs_truncated")
                        or detail.get("workflow_jobs_truncated")
                        or detail.get("workflow_steps_truncated")
                    )
                    if detail
                    else False
                ),
                "source": {
                    "kind": "github_pull_request",
                    "url": pr.get("html_url"),
                    "observed_at": detail["detail_observed_at"] if detail else None,
                },
            }
        )

    return {
        "repository": {
            "full_name": repo_result.get("full_name"),
            "url": repo_result.get("html_url"),
            "default_branch": repo_result.get("default_branch"),
            "private": repo_result.get("private"),
            "availability": "available",
        },
        "pull_requests": pull_requests,
        "closed_pull_requests": [
            {
                "number": int(pr["number"]),
                "state": "merged" if pr.get("merged_at") else "closed_unmerged",
                "url": pr.get("html_url"),
                "updated_at": pr.get("updated_at"),
                "head_branch": (pr.get("head") or {}).get("ref"),
                "head_sha": (pr.get("head") or {}).get("sha"),
            }
            for pr in closed_prs
        ],
        "pagination": {
            "open_pull_requests_truncated": open_result.truncated,
            "open_pull_request_pages": open_result.pages_fetched,
            "closed_history_truncated": closed_result.truncated,
            "closed_history_pages": closed_result.pages_fetched,
            "detailed_pull_request_count": len(detail_prs),
            "total_open_pull_request_count": len(open_prs),
            "detail_limit": MAX_DETAILED_PULL_REQUESTS,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def _get_github_observation(force_refresh: bool) -> dict[str, Any]:
    settings = get_settings()
    repo = settings.github_repo or ""
    if not settings.github_token or not repo:
        return {
            "repository": {
                "full_name": repo or None,
                "url": f"https://github.com/{repo}" if repo else None,
                "availability": "not_configured",
            },
            "pull_requests": [],
            "closed_pull_requests": [],
            "pagination": {},
            "observed_at": None,
            "freshness": "unavailable",
            "error_category": "github_credentials_not_configured",
        }
    cached = _SNAPSHOT_CACHE.get(repo)
    now = time.monotonic()
    if not force_refresh and cached and now - cached[0] <= SNAPSHOT_TTL_SECONDS:
        result = copy.deepcopy(cached[1])
        result["freshness"] = "cached_fresh"
        result["cache_age_seconds"] = int(now - cached[0])
        return result
    try:
        observation = await _fetch_github_observation(GitHubClient())
        _SNAPSHOT_CACHE[repo] = (time.monotonic(), copy.deepcopy(observation))
        observation["freshness"] = "live"
        observation["cache_age_seconds"] = 0
        return observation
    except (GitHubClientError, httpx.RequestError, ValueError):
        if cached:
            observation = copy.deepcopy(cached[1])
            observation["freshness"] = "stale"
            observation["cache_age_seconds"] = int(now - cached[0])
            observation["error_category"] = "github_refresh_failed"
            observation["repository"]["availability"] = "stale_cache"
            return observation
        return {
            "repository": {
                "full_name": repo,
                "url": f"https://github.com/{repo}",
                "availability": "unavailable",
            },
            "pull_requests": [],
            "closed_pull_requests": [],
            "pagination": {},
            "observed_at": None,
            "freshness": "unavailable",
            "error_category": "github_api_unavailable",
        }
def _local_project_state(db: Session, owner_id: UUID) -> dict[str, Any]:
    capabilities = list_capability_records(db, owner_id=owner_id)
    work_candidates = list(
        db.execute(
            select(WorkCandidate)
            .where(WorkCandidate.owner_id == owner_id)
            .order_by(WorkCandidate.updated_at.desc())
            .limit(20)
        ).scalars()
    )
    agents = all_agents_runtime_snapshot(db, owner_id=owner_id)
    assignments = work_registry_snapshot(db, owner_id=owner_id)
    agent_result_evidence = list(
        db.execute(
            select(IntelligenceEvidence)
            .where(
                IntelligenceEvidence.owner_id == owner_id,
                IntelligenceEvidence.source_type == "agent_coordination",
                IntelligenceEvidence.evidence_kind == "dispatch_result",
            )
            .order_by(IntelligenceEvidence.observed_at.desc())
            .limit(20)
        ).scalars()
    )
    active_goal_statuses = tuple(status for status in MainAIGoalStatus if status not in {
        MainAIGoalStatus.failed, MainAIGoalStatus.completed, MainAIGoalStatus.cancelled
    })
    goals = list(
        db.execute(
            select(MainAIGoal)
            .where(MainAIGoal.owner_id == owner_id, MainAIGoal.status.in_(active_goal_statuses))
            .order_by(MainAIGoal.created_at.desc())
            .limit(50)
        ).scalars()
    )
    tasks = list(
        db.execute(
            select(MainAITask)
            .where(
                MainAITask.owner_id == owner_id,
                MainAITask.status.notin_(
                    (MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled)
                ),
            )
            .order_by(MainAITask.created_at.desc())
            .limit(100)
        ).scalars()
    )
    task_ids = [task.id for task in tasks]
    dependencies = []
    if task_ids:
        dependencies = list(
            db.execute(
                select(MainAITaskDependency).where(
                    MainAITaskDependency.owner_id == owner_id,
                    MainAITaskDependency.task_id.in_(task_ids),
                )
            ).scalars()
        )
    dependency_map: dict[UUID, list[str]] = {}
    for dependency in dependencies:
        dependency_map.setdefault(dependency.task_id, []).append(str(dependency.depends_on_task_id))

    task_summaries = [
        {
            "id": str(task.id),
            "goal_id": str(task.goal_id),
            "description": _safe_text(task.description, 500),
            "task_type": task.task_type,
            "status": task.status.value,
            "priority": task.priority,
            "blocker_reason": _safe_text(task.blocker_reason, 500) if task.blocker_reason else None,
            "depends_on": dependency_map.get(task.id, []),
            "source": {
                "kind": "mainai_task",
                "id": str(task.id),
                "api_path": f"/api/mainai/execution/tasks/{task.id}",
                "href": f"#task-{task.id}",
            },
        }
        for task in tasks
    ]
    next_work = [
        {
            "kind": "mainai_task",
            "id": str(task.id),
            "title": _safe_text(task.description, 240),
            "reason": "Task is in the canonical ready state; this observation does not dispatch it.",
            "source": f"/api/mainai/execution/tasks/{task.id}",
            "status": "feasible_candidate_not_dispatched",
        }
        for task in tasks
        if task.status == MainAITaskStatus.ready
    ]
    return {
        "capabilities": [
            {
                "id": str(item.id),
                "key": item.capability_key,
                "domain": item.domain,
                "status": item.status,
                "status_reason": _safe_text(item.status_reason, 500) if item.status_reason else None,
                "last_verified_at": _iso(item.last_verified_at),
                "verification_evidence_id": str(item.last_verification_evidence_id)
                if item.last_verification_evidence_id
                else None,
                "known_limitations": item.known_limitations or [],
                "source": {
                    "kind": "capability_record",
                    "id": str(item.id),
                    "api_path": "/api/github-operations/snapshot",
                    "href": f"#capability-{item.id}",
                },
                "test_state": "evidence_recorded" if item.last_verification_evidence_id else "unknown",
            }
            for item in capabilities
        ],
        "agents": [
            {
                "id": str(agent.agent_id),
                "key": agent.agent_key,
                "name": agent.display_name,
                "registry_status": agent.registry_status,
                "runtime_status": agent.runtime_status.value,
                "availability_reason": _safe_text(agent.availability_reason, 300),
                "adapter_kind": agent.adapter_kind,
                "heartbeat_at": _iso(agent.heartbeat_at),
                "source": {
                    "kind": "coordination_agent",
                    "id": str(agent.agent_id),
                    "href": "/admin/agents",
                },
            }
            for agent in agents
        ],
        "assignments": [
            {
                "id": str(item.assignment_id),
                "agent_id": str(item.agent_id),
                "agent_key": item.agent_key,
                "role": item.role,
                "status": item.canonical_status,
                "runtime_status": item.runtime_status.value,
                "branch": item.branch,
                "head_sha": item.head_sha,
                "repository": item.repository_identity,
                "block_reason": _safe_text(item.block_reason, 500) if item.block_reason else None,
                "created_at": None,
                "source": {
                    "kind": "agent_work_assignment",
                    "id": str(item.assignment_id),
                    "href": "/admin/agents",
                },
            }
            for item in assignments
        ],
        "agent_results": [_agent_result_summary(item) for item in agent_result_evidence],
        "goals": [
            {
                "id": str(goal.id),
                "title": _safe_text(goal.title, 240),
                "status": goal.status.value,
                "created_at": _iso(goal.created_at),
                "source": {
                    "kind": "mainai_goal",
                    "id": str(goal.id),
                    "api_path": f"/api/mainai/execution/goals/{goal.id}",
                    "href": f"/api/mainai/execution/goals/{goal.id}",
                },
            }
            for goal in goals
        ],
        "tasks": task_summaries,
        "work_candidates": [
            {
                "id": str(candidate.id),
                "title": _safe_text(candidate.title, 300),
                "status": candidate.status,
                "priority": candidate.priority,
                "classifier_confidence": candidate.classifier_confidence,
                "observed_at": _iso(candidate.observed_at),
                "rationale": _safe_text(candidate.rationale, 500) if candidate.rationale else None,
                "why": why_feature_exists(
                    db, owner_id=owner_id, work_candidate_id=candidate.id
                ),
                "decision_completeness": assess_decision_completeness(
                    provenance=candidate.provenance,
                ),
                "source": {
                    "kind": "work_candidate",
                    "id": str(candidate.id),
                    "href": f"#candidate-{candidate.id}",
                },
            }
            for candidate in work_candidates
        ],
        "decision_debt": list_decision_debt(db, owner_id=owner_id, limit=20),
        "next_work": next_work,
    }


async def build_project_snapshot(
    db: Session,
    *,
    owner_id: UUID,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build the read-only response for GET /api/github-operations/snapshot.

    Freshness is explicit (`live`, `cached_fresh`, `stale`, or `unavailable`). The in-memory
    cache is bounded by the configured TTL and is never represented as a live observation.
    """
    github = await _get_github_observation(force_refresh)
    local = _local_project_state(db, owner_id)
    pull_requests = github.get("pull_requests", [])
    blockers: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    for pr in pull_requests:
        if pr["dependencies"] and all(not dependency["blocked"] for dependency in pr["dependencies"]):
            recommendations.append(
                {
                    "kind": "dependency_unblocked",
                    "title": f"PR #{pr['number']} dependency is merged",
                    "status": "unblocked_candidate",
                    "source": pr["url"],
                    "reason": "Every explicitly declared dependency in this PR body is observed as merged; this does not assert tests or review are complete.",
                    "decision_completeness": assess_decision_completeness(
                        provenance=None,
                        category="project_work_priority",
                    ),
                }
            )
        for dependency in pr["dependencies"]:
            if dependency["blocked"]:
                blockers.append(
                    {
                        "kind": "pull_request_dependency",
                        "status": (
                            "blocked"
                            if dependency["state"] == "open"
                            else "blocked_dependency_closed_unmerged"
                            if dependency["state"] == "closed_unmerged"
                            else "unknown"
                        ),
                        "title": f"PR #{pr['number']} depends on PR #{dependency['number']}",
                        "source": dependency["url"],
                        "evidence": dependency["source"],
                    }
                )
        for check in pr["checks"]:
            if check.get("is_latest_for_check", True) and check["state"] == "failed":
                blockers.append(
                    {
                        "kind": "failed_check",
                        "status": "failed",
                        "title": f"PR #{pr['number']}: {check['name'] or 'check'} failed",
                        "source": check["url"] or pr["url"],
                        "evidence": "GitHub check-run observation",
                    }
                )
        for run in pr["workflow_runs"]:
            if not run.get("is_latest_for_workflow"):
                continue
            if _check_state(run) == "failed":
                blockers.append(
                    {
                        "kind": "failed_workflow",
                        "status": "failed",
                        "title": f"PR #{pr['number']}: {run['name'] or 'workflow'} failed",
                        "source": run["url"] or pr["url"],
                        "evidence": "GitHub Actions workflow-run observation",
                    }
                )
            for job in run["jobs"]:
                if job["state"] == "failed":
                    blockers.append(
                        {
                            "kind": "failed_workflow_job",
                            "status": "failed",
                            "title": f"PR #{pr['number']}: {job['name'] or 'workflow job'} failed",
                            "source": job["url"] or run["url"] or pr["url"],
                            "evidence": "GitHub Actions job observation",
                        }
                    )
        if pr["review_state"] == "changes_requested":
            blockers.append(
                {
                    "kind": "review_changes_requested",
                    "status": "blocked",
                    "title": f"PR #{pr['number']} has requested changes",
                    "source": pr["url"],
                    "evidence": "Latest submitted review state",
                }
            )
        elif not pr["draft"] and pr["review_state"] != "approved_exact_head":
            recommendations.append(
                {
                    "kind": "review",
                    "title": f"Verify current-head review for PR #{pr['number']}",
                    "status": "review_needed_or_not_verified",
                    "source": pr["url"],
                    "reason": (
                        f"Current-head review evidence is {pr['review_state']}; this is not an independent review or release approval."
                    ),
                    "decision_completeness": assess_decision_completeness(
                        provenance=None,
                        category="project_work_priority",
                    ),
                }
            )
    for task in local["tasks"]:
        if task["status"] == "blocked":
            blockers.append(
                {
                    "kind": "mainai_task",
                    "status": "blocked",
                    "title": task["description"][:180],
                    "source": task["source"]["api_path"],
                    "evidence": task["blocker_reason"] or "Canonical task status is blocked.",
                }
            )
    recommendations.extend(
        {
            **item,
            "decision_completeness": assess_decision_completeness(
                provenance=None,
                category="project_work_priority",
            ),
        }
        for item in local["next_work"]
    )
    return {
        "schema_version": 1,
        "repository": github["repository"],
        "observation": {
            "freshness": github["freshness"],
            "observed_at": github["observed_at"],
            "cache_age_seconds": github.get("cache_age_seconds"),
            "error_category": github.get("error_category"),
            "pagination": github.get("pagination", {}),
        },
        "pull_requests": pull_requests,
        "closed_pull_requests": github.get("closed_pull_requests", []),
        "capabilities": local["capabilities"],
        "agents": local["agents"],
        "assignments": local["assignments"],
        "agent_results": local["agent_results"],
        "goals": local["goals"],
        "tasks": local["tasks"],
        "work_candidates": local["work_candidates"],
        "decision_debt": local["decision_debt"],
        "blockers": blockers,
        "recommendations": recommendations,
        "activity": [
            {
                "kind": "pull_request_updated",
                "title": f"PR #{pr['number']}: {pr['title']}",
                "timestamp": pr["updated_at"],
                "source": pr["url"],
                "head_sha": pr["head_sha"],
            }
            for pr in sorted(pull_requests, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:20]
        ],
    }
