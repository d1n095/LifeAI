from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations import github_operations
from app.integrations.decision_completeness import PRIORITY_DIMENSIONS, assess_decision_completeness
from app.integrations.github_client import GitHubClient, GitHubClientError, GitHubListResult
from app.integrations.github_operations import _check_state, _fetch_github_observation, build_project_snapshot


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
        return {"mergeable": None, "mergeable_state": "unknown"}

    async def list_pull_request_reviews(self, number):
        reviews = (
            [{"id": number, "user": {"login": "reviewer"}, "state": "APPROVED", "submitted_at": "2026-11-09T13:00:00Z"}]
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
            if sha == self.head_sha
            else []
        )
        return GitHubListResult(checks, 1, False)

    async def list_workflow_runs(self, sha):
        if sha != self.head_sha:
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
    assert pr_by_number[101]["workflow_runs"][0]["jobs"][0]["state"] == "failed"
    assert pr_by_number[101]["dependencies"][0]["state"] == "open"
    assert pr_by_number[102]["dependencies"][0]["state"] == "merged"
    assert pr_by_number[103]["draft"] is True
    assert any(blocker["kind"] == "failed_check" for blocker in snapshot["blockers"])
    assert any(blocker["kind"] == "failed_workflow_job" for blocker in snapshot["blockers"])
    assert any(item["kind"] == "dependency_unblocked" for item in snapshot["recommendations"])
    assert all(comment["untrusted_content"] for pr in pr_by_number.values() for comment in pr["comments"])

    fake.head_sha = "e" * 40
    fake.open_prs[0]["head"]["sha"] = fake.head_sha
    updated = await _fetch_github_observation(fake)
    assert updated["pull_requests"][0]["head_sha"] == "e" * 40


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
