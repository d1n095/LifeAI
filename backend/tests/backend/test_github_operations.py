from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations import github_operations
from app.integrations.decision_completeness import PRIORITY_DIMENSIONS, assess_decision_completeness
from app.integrations.github_client import GitHubClient, GitHubClientError, GitHubListResult
from app.integrations.github_operations import (
    _agent_result_summary,
    _aggregate_ci_state,
    _check_state,
    _discover_pull_request_details,
    _fetch_github_observation,
    _review_state,
    build_project_snapshot,
)


def _client(monkeypatch) -> GitHubClient:
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", "test-token")
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    return client


@pytest.mark.asyncio
async def test_paginated_pull_request_discovery_fetches_all_pages_with_read_only_requests(monkeypatch):
    calls = []

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        parsed = urlsplit(str(url))
        query = parse_qs(parsed.query)
        calls.append((method, parsed.path, query))
        page = int(query["page"][0])
        body = (
            [{"number": 1}, {"number": 2}]
            if page == 1
            else [{"number": 3}]
        )
        return httpx.Response(200, request=httpx.Request(method, url), json=body)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    result = await _client(monkeypatch).list_pull_requests(state="open", max_pages=2, page_size=2)

    assert [item["number"] for item in result.items] == [1, 2, 3]
    assert result.pages_fetched == 2
    assert result.truncated is False
    assert [call[0] for call in calls] == ["GET", "GET"]
    assert [call[2]["page"][0] for call in calls] == ["1", "2"]
    assert all(call[2]["per_page"][0] == "2" for call in calls)


@pytest.mark.asyncio
async def test_pagination_reports_truncation_at_configured_bound(monkeypatch):
    calls = []

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        calls.append(method)
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json=[{"number": len(calls) * 2 - 1}, {"number": len(calls) * 2}],
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    result = await _client(monkeypatch).list_pull_requests(state="open", max_pages=2, page_size=2)
    assert len(result.items) == 4
    assert result.truncated is True


@pytest.mark.asyncio
async def test_rate_limit_is_retried_and_never_returns_github_error_payload(monkeypatch):
    attempts = 0

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        nonlocal attempts
        attempts += 1
        request = httpx.Request(method, url)
        if attempts == 1:
            return httpx.Response(429, request=request, json={"message": "rate limited"})
        return httpx.Response(200, request=request, json=[])

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr("app.integrations.github_client.asyncio.sleep", no_wait)
    result = await _client(monkeypatch).list_pull_requests(state="open", max_pages=1)
    assert result.items == []
    assert attempts == 2


@pytest.mark.asyncio
async def test_rate_limit_retry_after_is_respected_without_long_blocking_or_early_retry(monkeypatch):
    attempts = 0

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            request=httpx.Request(method, url),
            headers={"Retry-After": "30"},
            json={"message": "private rate-limit response"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(GitHubClientError) as error:
        await _client(monkeypatch).list_pull_requests(state="open", max_pages=1)
    assert attempts == 1
    assert "private rate-limit response" not in str(error.value)


@pytest.mark.asyncio
async def test_transient_network_failures_retry_with_a_safe_final_error(monkeypatch):
    attempts = 0

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("sensitive network detail", request=httpx.Request(method, url))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr("app.integrations.github_client.asyncio.sleep", no_wait)
    with pytest.raises(GitHubClientError) as error:
        await _client(monkeypatch).list_pull_requests(state="open", max_pages=1)
    assert attempts == GitHubClient.MAX_READ_ATTEMPTS
    assert "sensitive network detail" not in str(error.value)


@pytest.mark.asyncio
async def test_missing_github_credentials_fail_before_network(monkeypatch):
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", None)
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    calls = []

    async def fake_request(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(GitHubClientError):
        await client.list_pull_requests(state="open")
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_repository_configuration_fails_before_network(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI?redirect=example.invalid")
    calls = []

    async def fake_request(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(GitHubClientError):
        await client.list_pull_requests(state="open")
    assert calls == []


@pytest.mark.asyncio
async def test_project_discovery_uses_only_read_only_http_requests(monkeypatch):
    client = _client(monkeypatch)
    calls = []
    head_sha = "a" * 40

    async def fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        parsed = urlsplit(str(url))
        calls.append((method, parsed.path))
        assert headers["Authorization"].startswith("Bearer ")
        if parsed.path == "/repos/d1n095/LifeAI":
            body = {
                "full_name": "d1n095/LifeAI",
                "html_url": "https://github.com/d1n095/LifeAI",
                "default_branch": "main",
            }
        elif parsed.path == "/repos/d1n095/LifeAI/pulls":
            body = [{
                "number": 1,
                "title": "Synthetic",
                "draft": False,
                "body": "",
                "html_url": "https://github.com/d1n095/LifeAI/pull/1",
                "user": {"login": "builder"},
                "head": {"ref": "feature/synthetic", "sha": head_sha, "repo": {"full_name": "d1n095/LifeAI"}},
                "base": {"ref": "main", "sha": "b" * 40},
                "updated_at": "2026-11-09T12:00:00Z",
            }] if parse_qs(parsed.query).get("state") == ["open"] else []
        elif parsed.path == "/repos/d1n095/LifeAI/pulls/1":
            body = {
                "head": {"ref": "feature/synthetic", "sha": head_sha},
                "base": {"ref": "main", "sha": "b" * 40},
                "mergeable": None,
                "mergeable_state": "unknown",
            }
        elif parsed.path == f"/repos/d1n095/LifeAI/commits/{head_sha}":
            body = {"sha": head_sha, "html_url": f"https://github.com/d1n095/LifeAI/commit/{head_sha}", "commit": {}}
        elif parsed.path == f"/repos/d1n095/LifeAI/commits/{head_sha}/check-runs":
            body = {"check_runs": []}
        elif parsed.path == "/repos/d1n095/LifeAI/actions/runs":
            body = {"workflow_runs": []}
        else:
            body = []
        return httpx.Response(200, request=httpx.Request(method, url), json=body)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    observation = await _fetch_github_observation(client)

    assert observation["repository"]["full_name"] == "d1n095/LifeAI"
    assert observation["pull_requests"][0]["head_sha"] == head_sha
    assert calls
    assert {method for method, _path in calls} == {"GET"}


def test_ci_conclusions_preserve_failed_cancelled_pending_skipped_and_success():
    cases = [
        ({"status": "completed", "conclusion": "failure"}, "failed"),
        ({"status": "completed", "conclusion": "cancelled"}, "cancelled"),
        ({"status": "in_progress", "conclusion": None}, "pending"),
        ({"status": "completed", "conclusion": "skipped"}, "skipped"),
        ({"status": "completed", "conclusion": "success"}, "success"),
        ({"status": "completed", "conclusion": None}, "unknown"),
    ]
    assert [(_check_state(check)) for check, _expected in cases] == [
        expected for _check, expected in cases
    ]


class FakeGitHub:
    def __init__(self, *, head_sha: str = "a" * 40):
        self.head_sha = head_sha
        self.detail_head_sha = head_sha
        self.recheck_head_sha: dict[int, str] = {}
        self.pull_request_calls: dict[int, int] = {}
        self.open_prs = [
            {
                "number": 101,
                "title": "Blocked candidate",
                "draft": False,
                "body": "Depends on #102",
                "html_url": "https://github.com/d1n095/LifeAI/pull/101",
                "user": {"login": "builder"},
                "head": {"ref": "feature/blocked", "sha": head_sha, "repo": {"full_name": "d1n095/LifeAI"}},
                "base": {"ref": "main", "sha": "b" * 40},
                "updated_at": "2026-11-09T12:00:00Z",
                "requested_reviewers": [{"login": "reviewer"}],
            },
            {
                "number": 102,
                "title": "Unblocked candidate",
                "draft": False,
                "body": "Stacked on #99",
                "html_url": "https://github.com/d1n095/LifeAI/pull/102",
                "user": {"login": "builder"},
                "head": {"ref": "feature/unblocked", "sha": "c" * 40, "repo": {"full_name": "d1n095/LifeAI"}},
                "base": {"ref": "main", "sha": "b" * 40},
                "updated_at": "2026-11-09T11:00:00Z",
                "requested_reviewers": [],
            },
            {
                "number": 103,
                "title": "Draft candidate",
                "draft": True,
                "body": "",
                "html_url": "https://github.com/d1n095/LifeAI/pull/103",
                "user": {"login": "builder"},
                "head": {"ref": "feature/draft", "sha": "d" * 40, "repo": {"full_name": "d1n095/LifeAI"}},
                "base": {"ref": "main", "sha": "b" * 40},
                "updated_at": "2026-11-09T10:00:00Z",
                "requested_reviewers": [],
            },
        ]

    async def get_repository(self):
        return {
            "full_name": "d1n095/LifeAI",
            "html_url": "https://github.com/d1n095/LifeAI",
            "default_branch": "main",
            "private": True,
        }

    async def list_pull_requests(self, *, state, **kwargs):
        if state == "open":
            return GitHubListResult(self.open_prs, 1, False)
        return GitHubListResult(
            [
                {
                    "number": 99,
                    "merged_at": "2026-11-08T10:00:00Z",
                    "html_url": "https://github.com/d1n095/LifeAI/pull/99",
                    "updated_at": "2026-11-08T10:00:00Z",
                },
                {
                    "number": 100,
                    "merged_at": None,
                    "html_url": "https://github.com/d1n095/LifeAI/pull/100",
                    "updated_at": "2026-11-08T09:00:00Z",
                },
            ],
            1,
            False,
        )

    async def get_pull_request(self, number):
        calls = self.pull_request_calls.get(number, 0) + 1
        self.pull_request_calls[number] = calls
        sha = self.recheck_head_sha.get(number, self.detail_head_sha) if calls > 1 else self.detail_head_sha
        pull_request = next(item for item in self.open_prs if item["number"] == number)
        return {
            "head": {"sha": sha, "ref": pull_request["head"]["ref"]},
            "base": {"sha": pull_request["base"]["sha"], "ref": pull_request["base"]["ref"]},
            "mergeable": None,
            "mergeable_state": "unknown",
        }

    async def list_pull_request_reviews(self, number, *, max_pages=1):
        reviews = (
            [{
                "id": number,
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "commit_id": self.detail_head_sha,
                "submitted_at": "2026-11-09T13:00:00Z",
            }]
            if number == 102
            else []
        )
        return GitHubListResult(reviews, 1, False)

    async def list_pull_request_comments(self, number):
        return GitHubListResult(
            [{"id": number, "user": {"login": "reviewer"}, "body": "Untrusted comment", "html_url": f"https://github.com/d1n095/LifeAI/pull/{number}#issuecomment-1"}],
            1,
            False,
        )

    async def list_pull_request_review_comments(self, number):
        return GitHubListResult([], 1, False)

    async def list_check_runs_result(self, sha):
        checks = (
            [{"name": "tests", "status": "completed", "conclusion": "failure", "html_url": "https://github.com/d1n095/LifeAI/actions/runs/1"}]
            if sha == self.detail_head_sha
            else []
        )
        return GitHubListResult(checks, 1, False)

    async def list_workflow_runs(self, sha):
        if sha != self.detail_head_sha:
            return GitHubListResult([], 1, False)
        return GitHubListResult(
            [{"id": 9001, "name": "CI", "status": "completed", "conclusion": "failure", "head_sha": sha, "html_url": "https://github.com/d1n095/LifeAI/actions/runs/9001"}],
            1,
            False,
        )

    async def list_workflow_jobs(self, run_id):
        return GitHubListResult(
            [{"id": 7001, "name": "Unit tests", "status": "completed", "conclusion": "failure", "html_url": "https://github.com/d1n095/LifeAI/actions/runs/9001/job/7001"}],
            1,
            False,
        )

    async def get_commit_metadata(self, sha):
        return {
            "sha": sha,
            "html_url": f"https://github.com/d1n095/LifeAI/commit/{sha}",
            "commit": {"message": "Synthetic commit", "committer": {"date": "2026-11-09T12:00:00Z"}},
        }


@pytest.mark.asyncio
async def test_snapshot_preserves_exact_sha_ci_dependencies_draft_and_unknown_mergeability(monkeypatch):
    fake = FakeGitHub()
    observation = await _fetch_github_observation(fake)
    observation.update(freshness="live", cache_age_seconds=0)
    monkeypatch.setattr(
        github_operations,
        "_local_project_state",
        lambda _db, _owner: {
            "capabilities": [],
            "agents": [],
            "assignments": [],
            "goals": [],
            "tasks": [],
            "work_candidates": [],
            "decision_debt": {"items": []},
            "agent_results": [],
            "next_work": [],
        },
    )
    monkeypatch.setattr(github_operations, "_get_github_observation", lambda _force: _async_value(observation))
    snapshot = await build_project_snapshot(object(), owner_id=__import__("uuid").uuid4())

    pr_by_number = {pr["number"]: pr for pr in snapshot["pull_requests"]}
    assert pr_by_number[101]["head_sha"] == "a" * 40
    assert pr_by_number[101]["base_branch"] == "main"
    assert pr_by_number[101]["mergeability"]["state"] == "UNKNOWN"
    assert pr_by_number[101]["checks"][0]["state"] == "failed"
    assert pr_by_number[101]["ci_state"] == "failed"
    assert pr_by_number[101]["workflow_runs"][0]["jobs"][0]["state"] == "failed"
    assert pr_by_number[101]["dependencies"][0]["state"] == "open"
    assert pr_by_number[102]["dependencies"][0]["state"] == "merged"
    assert pr_by_number[102]["review_state"] == "approved_exact_head"
    assert pr_by_number[102]["reviews"][0]["commit_sha"] == pr_by_number[102]["head_sha"]
    assert pr_by_number[103]["draft"] is True
    assert pr_by_number[103]["dependency_evidence"]["state"] == "not_declared_or_unknown"
    assert any(blocker["kind"] == "failed_check" for blocker in snapshot["blockers"])
    assert any(blocker["kind"] == "failed_workflow_job" for blocker in snapshot["blockers"])
    assert any(item["kind"] == "dependency_unblocked" for item in snapshot["recommendations"])
    assert all(comment["untrusted_content"] for pr in pr_by_number.values() for comment in pr["comments"])

    fake.head_sha = "e" * 40
    fake.detail_head_sha = fake.head_sha
    fake.open_prs[0]["head"]["sha"] = fake.head_sha
    updated = await _fetch_github_observation(fake)
    assert updated["pull_requests"][0]["head_sha"] == "e" * 40


@pytest.mark.asyncio
async def test_head_change_between_list_and_detail_uses_latest_sha_for_evidence():
    fake = FakeGitHub()
    fake.open_prs[0]["head"]["sha"] = "a" * 40
    fake.detail_head_sha = "e" * 40

    observation = await _fetch_github_observation(fake)
    pull_request = observation["pull_requests"][0]

    assert pull_request["head_sha"] == "e" * 40
    assert pull_request["head_sha_changed_since_list"] is True
    assert all(check["head_sha"] == pull_request["head_sha"] for check in pull_request["checks"])
    assert all(run["head_sha"] == pull_request["head_sha"] for run in pull_request["workflow_runs"])


@pytest.mark.asyncio
async def test_head_change_during_detail_invalidates_sha_bound_reviews_checks_and_runs():
    fake = FakeGitHub()
    fake.recheck_head_sha[101] = "f" * 40

    observation = await _fetch_github_observation(fake)
    pull_request = next(item for item in observation["pull_requests"] if item["number"] == 101)

    assert pull_request["head_sha"] == "f" * 40
    assert pull_request["head_sha_changed_during_detail"] is True
    assert pull_request["checks"] == []
    assert pull_request["workflow_runs"] == []
    assert pull_request["latest_commit"] is None
    assert pull_request["ci_state"] == "unknown"
    assert pull_request["review_state"] == "unknown_head_changed"


def test_review_state_requires_current_sha_and_complete_review_history():
    review = {
        "user": {"login": "reviewer"},
        "state": "APPROVED",
        "commit_id": "a" * 40,
        "submitted_at": "2026-11-09T13:00:00Z",
    }

    assert _review_state([review], head_sha="a" * 40, truncated=False) == "approved_exact_head"
    assert _review_state([review], head_sha="b" * 40, truncated=False) == "approval_stale_head"
    assert _review_state([{**review, "commit_id": None}], head_sha="a" * 40, truncated=False) == "approval_sha_unknown"
    assert _review_state([review], head_sha="a" * 40, truncated=True) == "unknown_truncated"


def test_ci_aggregate_failure_is_not_overridden_by_successful_individual_job():
    details = {
        "checks": [{"state": "success"}],
        "workflow_runs": [{
            "status": "completed",
            "conclusion": "failure",
            "jobs": [{"state": "success"}],
        }],
        "detail_errors": [],
    }

    assert _aggregate_ci_state(details) == "failed"


def test_ci_summary_uses_latest_check_and_workflow_for_each_name():
    details = {
        "checks": [
            {"name": "tests", "state": "success", "started_at": "2026-11-09T12:00:00Z", "is_latest_for_check": True},
            {"name": "tests", "state": "failed", "started_at": "2026-11-09T11:00:00Z", "is_latest_for_check": False},
        ],
        "workflow_runs": [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "is_latest_for_workflow": True,
                "jobs": [{"state": "success"}],
            },
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "is_latest_for_workflow": False,
                "jobs": [{"state": "failed"}],
            },
        ],
        "detail_errors": [],
    }

    assert _aggregate_ci_state(details) == "success"


@pytest.mark.asyncio
async def test_workflow_run_with_a_different_head_sha_is_not_used_as_current_evidence():
    fake = FakeGitHub()

    async def mismatched_runs(_sha):
        return GitHubListResult(
            [{
                "id": 9002,
                "name": "stale CI",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "f" * 40,
                "html_url": "https://github.com/d1n095/LifeAI/actions/runs/9002",
            }],
            1,
            False,
        )

    fake.list_workflow_runs = mismatched_runs
    pull_request = await _discover_pull_request_details(fake, fake.open_prs[0])

    assert pull_request["workflow_runs"] == []
    assert "workflow_run_head_sha_mismatch" in pull_request["detail_errors"]
    assert pull_request["ci_state"] == "failed"


def test_agent_result_projection_exposes_canonical_usage_without_claiming_verification():
    from types import SimpleNamespace

    evidence = SimpleNamespace(
        id="evidence-id",
        source_ref="assignment-id",
        evidence_kind="dispatch_result",
        review_kind="unknown",
        deterministic=True,
        observed_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        payload={
            "tests_passed": True,
            "tests_run": 12,
            "tests_failed": 0,
            "duration_seconds": 4.5,
            "cost_tokens": 1200,
            "cost_usd": "0.025",
            "ci_conclusion": "success",
            "failure_reason": "untrusted, potentially sensitive content",
        },
    )

    result = _agent_result_summary(evidence)

    assert result["verification_status"] == "not_independently_verified"
    assert result["cost_tokens"] == 1200
    assert result["cost_usd"] == 0.025
    assert result["tests_passed"] is True
    assert "failure_reason" not in result


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_github_snapshot_cache_distinguishes_live_fresh_stale_and_unavailable(monkeypatch):
    repo = "d1n095/LifeAI"
    monkeypatch.setattr(
        github_operations,
        "get_settings",
        lambda: SimpleNamespace(github_repo=repo, github_token="test-token"),
    )
    monkeypatch.setattr(github_operations, "_SNAPSHOT_CACHE", {})
    original_timestamp = "2026-11-09T12:00:00+00:00"
    payload = {
        "repository": {"full_name": repo, "availability": "available"},
        "pull_requests": [{"number": 44}],
        "closed_pull_requests": [],
        "pagination": {"open_pull_requests_truncated": False},
        "observed_at": original_timestamp,
    }

    async def successful_fetch(_client):
        return payload

    monkeypatch.setattr(github_operations, "_fetch_github_observation", successful_fetch)
    live = await github_operations._get_github_observation(force_refresh=True)
    assert live["freshness"] == "live"
    assert live["observed_at"] == original_timestamp
    cached = await github_operations._get_github_observation(force_refresh=False)
    assert cached["freshness"] == "cached_fresh"
    assert cached["observed_at"] == original_timestamp

    async def failed_fetch(_client):
        raise GitHubClientError("sanitized synthetic outage")

    monkeypatch.setattr(github_operations, "_fetch_github_observation", failed_fetch)
    stale = await github_operations._get_github_observation(force_refresh=True)
    assert stale["freshness"] == "stale"
    assert stale["observed_at"] == original_timestamp
    assert stale["error_category"] == "github_refresh_failed"

    monkeypatch.setattr(github_operations, "_SNAPSHOT_CACHE", {})
    missing = await github_operations._get_github_observation(force_refresh=True)
    assert missing["freshness"] == "unavailable"


@pytest.mark.asyncio
async def test_missing_github_configuration_is_explicit_and_does_not_attempt_discovery(monkeypatch):
    monkeypatch.setattr(
        github_operations,
        "get_settings",
        lambda: SimpleNamespace(github_repo="d1n095/LifeAI", github_token=None),
    )
    monkeypatch.setattr(github_operations, "_SNAPSHOT_CACHE", {})

    async def unexpected_fetch(_client):
        raise AssertionError("GitHub fetch should not run without credentials.")

    monkeypatch.setattr(github_operations, "_fetch_github_observation", unexpected_fetch)
    result = await github_operations._get_github_observation(force_refresh=True)
    assert result["freshness"] == "unavailable"
    assert result["error_category"] == "github_credentials_not_configured"
    assert result["pull_requests"] == []


def test_decision_completeness_never_scores_missing_or_assumed_priority_factors():
    incomplete = assess_decision_completeness(provenance=None)
    assert incomplete["status"] == "NOT_CHECKED"
    assert incomplete["priority"]["score"] is None
    assert set(incomplete["factors_missing"]) == set(PRIORITY_DIMENSIONS)
    assert set(incomplete["impact_unknown_for"]) == set(PRIORITY_DIMENSIONS)
    assert all(item["status"] == "NOT_CHECKED" for item in incomplete["mandatory_constraints"].values())
    assert incomplete["alternatives"]["status"] == "NOT_CHECKED"
    read_only = assess_decision_completeness(
        provenance=None,
        category="read_only_observation",
    )
    assert read_only["mandatory_constraints"]["founder_approval"]["status"] == "NOT_APPLICABLE"
    assert read_only["mandatory_constraints"]["security"]["status"] == "NOT_CHECKED"

    provenance = {
        "priority_factors": {
            name: {
                "value": 0.5,
                "source_ref": f"test://{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in PRIORITY_DIMENSIONS
        },
        "constraints": {
            name: {
                "status": "CHECKED",
                "source_ref": f"test://constraint/{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in ("security", "authorization", "dependencies", "founder_approval")
        },
        "alternatives": [
            {
                "description": name,
                "source_ref": f"test://alternative/{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in ("option-a", "option-b")
        ],
    }
    complete = assess_decision_completeness(provenance=provenance)
    assert complete["priority"]["status"] == "SCORED"
    assert complete["priority"]["engine"] == "app.mainai_executive.priority.score_priority"
    assert complete["alternatives"]["status"] == "EVALUATED"
    assert complete["authority_granted"] is False


def test_decision_completeness_marks_stale_and_freshness_unknown_without_scoring():
    provenance = {
        "priority_factors": {
            name: {
                "value": 0.5,
                "source_ref": f"test://{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in PRIORITY_DIMENSIONS
        },
        "alternatives": ["option-a"],
    }
    provenance["priority_factors"][PRIORITY_DIMENSIONS[0]]["valid_until"] = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    provenance["priority_factors"][PRIORITY_DIMENSIONS[1]].pop("freshness_status")
    result = assess_decision_completeness(provenance=provenance)
    assert PRIORITY_DIMENSIONS[0] in result["factors_stale"]
    assert PRIORITY_DIMENSIONS[1] in result["factors_freshness_unknown"]
    assert result["priority"]["score"] is None


def test_not_applicable_factor_is_distinct_and_prevents_default_scoring():
    provenance = {
        "priority_factors": {
            name: {
                "value": 0.5,
                "source_ref": f"test://{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in PRIORITY_DIMENSIONS
        },
        "alternatives": [],
    }
    not_applicable = PRIORITY_DIMENSIONS[0]
    provenance["priority_factors"][not_applicable] = {"freshness_status": "NOT_APPLICABLE"}
    result = assess_decision_completeness(provenance=provenance)
    assert not_applicable in result["factors_not_applicable"]
    assert not_applicable not in result["factors_missing"]
    assert not_applicable not in result["impact_unknown_for"]
    assert result["priority"]["score"] is None


def test_explicit_stale_freshness_is_not_reclassified_as_unknown():
    provenance = {
        "priority_factors": {
            name: {
                "value": 0.5,
                "source_ref": f"test://{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in PRIORITY_DIMENSIONS
        },
        "alternatives": ["option-a"],
    }
    stale_factor = PRIORITY_DIMENSIONS[0]
    provenance["priority_factors"][stale_factor]["freshness_status"] = "STALE"
    result = assess_decision_completeness(provenance=provenance)
    assert stale_factor in result["factors_stale"]
    assert stale_factor not in result["factors_freshness_unknown"]
    assert result["priority"]["score"] is None


def test_invalid_or_unattributed_priority_inputs_never_score():
    provenance = {
        "priority_factors": {
            name: {
                "value": 0.5,
                "source_ref": f"test://{name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness_status": "CURRENT",
            }
            for name in PRIORITY_DIMENSIONS
        },
        "alternatives": [],
    }
    provenance["priority_factors"][PRIORITY_DIMENSIONS[0]]["value"] = float("nan")
    provenance["founder_override"] = "NOW"
    result = assess_decision_completeness(provenance=provenance)
    assert PRIORITY_DIMENSIONS[0] in result["factors_assumed_or_unsourced"]
    assert "founder_override" in result["factors_assumed_or_unsourced"]
    assert result["priority"]["score"] is None

    provenance["priority_factors"][PRIORITY_DIMENSIONS[0]]["value"] = {"malformed": True}
    provenance["founder_override"] = {"value": [], "source_ref": "x", "observed_at": "y"}
    malformed = assess_decision_completeness(provenance=provenance)
    assert malformed["priority"]["score"] is None


def _login(client, email="founder@lifeos.local", password="TestFounderPassword123!"):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_github_operations_api_requires_founder(client, make_verified_user):
    assert client.get("/api/github-operations/snapshot").status_code == 401
    user, password = make_verified_user(role="member")
    _login(client, email=user.email, password=password)
    assert client.get("/api/github-operations/snapshot").status_code == 403


def test_github_operations_api_returns_snapshot_for_founder(client, monkeypatch):
    _login(client)
    expected = {
        "schema_version": 1,
        "repository": {"full_name": "d1n095/LifeAI", "url": "https://github.com/d1n095/LifeAI", "availability": "available"},
        "observation": {"freshness": "live", "observed_at": "2026-11-09T12:00:00Z"},
        "pull_requests": [],
    }

    async def fake_snapshot(db, *, owner_id, force_refresh=False):
        assert db is not None
        from uuid import UUID

        assert isinstance(owner_id, UUID)
        return expected

    monkeypatch.setattr("app.routers.github_operations.build_project_snapshot", fake_snapshot)
    response = client.get("/api/github-operations/snapshot?force_refresh=true")
    assert response.status_code == 200
    assert response.json() == expected
