"""Read-only, bounded project intelligence over GitHub and existing LifeAI records.

The GitHub payload is an observation, not an instruction source. This module never dispatches
agents or writes to GitHub; recommendations are deterministic rules over explicit evidence.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
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
MAX_WORKFLOW_JOBS_PER_PULL_REQUEST = 2
MAX_COMMENT_LENGTH = 800
_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DEPENDENCY_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:depends\s+on|stacked\s+on|blocked\s+by|requires)"
    r"\s+(?:pull\s+request\s+)?#(\d+)\b"
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


def _list_items(result: GitHubListResult) -> list[dict]:
    return result.items


def _review_state(reviews: list[dict]) -> str:
    latest_by_reviewer: dict[str, dict] = {}
    for review in reviews:
        reviewer = (review.get("user") or {}).get("login")
        if not reviewer or review.get("state") in {"PENDING", "COMMENTED"}:
            continue
        previous = latest_by_reviewer.get(reviewer)
        if previous is None or str(review.get("submitted_at") or "") >= str(previous.get("submitted_at") or ""):
            latest_by_reviewer[reviewer] = review
    states = {item.get("state") for item in latest_by_reviewer.values()}
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
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


async def _discover_pull_request_details(client: GitHubClient, pull_request: dict) -> dict[str, Any]:
    number = int(pull_request["number"])
    sha = str((pull_request.get("head") or {}).get("sha") or "")
    result: dict[str, Any] = {
        "reviews": [],
        "comments": [],
        "checks": [],
        "workflow_runs": [],
        "latest_commit": None,
        "mergeability": {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None},
        "detail_errors": [],
        "detail_observed_at": datetime.now(timezone.utc).isoformat(),
    }

    calls: dict[str, Any] = {
        "pull_request": client.get_pull_request(number),
        "reviews": client.list_pull_request_reviews(number),
        "comments": client.list_pull_request_comments(number),
        "review_comments": client.list_pull_request_review_comments(number),
    }
    if sha and re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        calls["checks"] = client.list_check_runs_result(sha)
        calls["workflow_runs"] = client.list_workflow_runs(sha)
        calls["latest_commit"] = client.get_commit_metadata(sha)

    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    data = dict(zip(calls, results))
    for name, value in data.items():
        if isinstance(value, asyncio.CancelledError):
            raise value
        if isinstance(value, Exception):
            result["detail_errors"].append(f"{name}_unavailable")
            continue
        if name == "pull_request":
            mergeable = value.get("mergeable")
            mergeable_state = value.get("mergeable_state")
            state = "UNKNOWN" if mergeable is None else ("MERGEABLE" if mergeable else "CONFLICTING")
            result["mergeability"] = {
                "state": state,
                "mergeable": mergeable,
                "mergeable_state": mergeable_state,
                "observed_at": result["detail_observed_at"],
            }
        elif name == "reviews":
            result["reviews"] = [
                {
                    "id": item.get("id"),
                    "author": (item.get("user") or {}).get("login"),
                    "state": item.get("state", "UNKNOWN"),
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
            result["checks"] = [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                    "state": _check_state(item),
                    "url": item.get("html_url") or item.get("details_url"),
                    "started_at": item.get("started_at"),
                    "completed_at": item.get("completed_at"),
                }
                for item in value.items
            ]
            result["checks_truncated"] = value.truncated
        elif name == "workflow_runs":
            runs = _list_items(value)[:MAX_WORKFLOW_JOBS_PER_PULL_REQUEST]
            result["workflow_jobs_truncated"] = False
            jobs_per_run = await asyncio.gather(
                *(client.list_workflow_jobs(int(run["id"])) for run in runs),
                return_exceptions=True,
            )
            for run, jobs_result in zip(runs, jobs_per_run):
                jobs = []
                if isinstance(jobs_result, asyncio.CancelledError):
                    raise jobs_result
                if isinstance(jobs_result, Exception):
                    result["detail_errors"].append("workflow_jobs_unavailable")
                else:
                    result["workflow_jobs_truncated"] = (
                        result["workflow_jobs_truncated"] or jobs_result.truncated
                    )
                    jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "state": _check_state(job),
                            "url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
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
                        "created_at": run.get("created_at"),
                        "updated_at": run.get("updated_at"),
                        "head_sha": run.get("head_sha"),
                        "jobs": jobs,
                    }
                )
            result["workflow_runs_truncated"] = value.truncated or len(value.items) > len(runs)
        elif name == "latest_commit":
            commit = value.get("commit") or {}
            committer = commit.get("committer") or {}
            result["latest_commit"] = {
                "sha": value.get("sha"),
                "message": _safe_text(commit.get("message"), 500),
                "committed_at": committer.get("date"),
                "url": value.get("html_url"),
            }
    result["review_state"] = _review_state(result["reviews"])
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
                    "review_state": "unknown",
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
        for dependency_number in _dependency_numbers(pr.get("body")):
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
                "head_branch": head.get("ref"),
                "head_sha": head.get("sha"),
                "head_repository": (head.get("repo") or {}).get("full_name"),
                "base_branch": base.get("ref"),
                "base_sha": (base.get("sha")),
                "updated_at": pr.get("updated_at"),
                "created_at": pr.get("created_at"),
                "dependencies": dependencies,
                "mergeability": (
                    detail["mergeability"]
                    if detail
                    else {"state": "UNKNOWN", "mergeable": None, "mergeable_state": None}
                ),
                "review_state": detail["review_state"] if detail else "unknown_not_fetched",
                "requested_reviewers": [
                    (reviewer or {}).get("login") for reviewer in pr.get("requested_reviewers", [])
                ],
                "reviews": detail["reviews"] if detail else [],
                "comments": detail["comments"] if detail else [],
                "checks": detail["checks"] if detail else [],
                "workflow_runs": detail["workflow_runs"] if detail else [],
                "latest_commit": detail["latest_commit"] if detail else None,
                "detail_errors": detail["detail_errors"] if detail else ["not_fetched_resource_limit"],
                "details_truncated": (
                    bool(
                        detail.get("reviews_truncated")
                        or detail.get("comments_truncated")
                        or detail.get("checks_truncated")
                        or detail.get("workflow_runs_truncated")
                        or detail.get("workflow_jobs_truncated")
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
            if check["state"] == "failed":
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
        elif not pr["draft"] and pr["review_state"] in {"pending_or_not_requested", "unknown_not_fetched"}:
            recommendations.append(
                {
                    "kind": "review",
                    "title": f"Review PR #{pr['number']}",
                    "status": "review_needed_or_not_verified",
                    "source": pr["url"],
                    "reason": "No current approval was observed in the fetched review history.",
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
