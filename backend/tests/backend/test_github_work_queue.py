"""app/safe_planner/github_work_queue.py — the stateless, read-only evidence report (Section B,
Continuous Build Directive). Same httpx-mocking technique as test_github_client.py (real
httpx.Response objects, network never touched) so these tests exercise the real GitHubClient
calls underneath, not a mocked build_github_work_queue_report."""

from datetime import UTC, datetime

import httpx
import pytest

from app.integrations.github_client import GitHubClient
from app.safe_planner.github_work_queue import build_github_work_queue_report

FIXED_NOW = datetime(2026, 11, 14, tzinfo=UTC)


def _fake_github(monkeypatch, responses: dict[tuple[str, str], dict | list]):
    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        path = str(url).removeprefix("https://api.github.com")
        request = httpx.Request(method, url)
        key = (method, path)
        if key not in responses:
            return httpx.Response(404, request=request, json={"message": f"unexpected call: {method} {path}"})
        return httpx.Response(200, request=request, json=responses[key])

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)


def _configured_client(monkeypatch) -> GitHubClient:
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    return client


def _pr(number, *, updated_at="2026-11-13T00:00:00Z", draft=False, head_sha="sha1", mergeable_state="clean", labels=()):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/d1n095/LifeAI/pull/{number}",
        "updated_at": updated_at,
        "draft": draft,
        "head": {"sha": head_sha},
        "mergeable_state": mergeable_state,
        "labels": [{"name": name} for name in labels],
    }


@pytest.mark.asyncio
async def test_fresh_pr_with_no_check_runs_has_no_signals(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(1, updated_at="2026-11-13T00:00:00Z")],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert len(report) == 1
    assert report[0].kind == "open_pull_request"
    assert report[0].signals == ()


@pytest.mark.asyncio
async def test_draft_pr_is_flagged_draft(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(2, draft=True)],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert report[0].draft is True
    assert "draft" in report[0].signals


@pytest.mark.asyncio
async def test_old_pr_is_flagged_stale(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(3, updated_at="2026-10-01T00:00:00Z")],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, stale_days=7, now=FIXED_NOW)

    assert "stale_7d" in report[0].signals


@pytest.mark.asyncio
async def test_pr_with_a_failing_check_run_is_flagged_ci_red(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(4)],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "completed", "conclusion": "failure"},
                ]
            },
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert "ci_red" in report[0].signals
    assert "ci_green" not in report[0].signals


@pytest.mark.asyncio
async def test_pr_with_all_passing_check_runs_is_flagged_ci_green(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(5)],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert "ci_green" in report[0].signals
    assert "ci_red" not in report[0].signals


@pytest.mark.asyncio
async def test_dirty_mergeable_state_is_flagged_merge_conflict(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(6, mergeable_state="dirty")],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert "merge_conflict" in report[0].signals


@pytest.mark.asyncio
async def test_labels_are_extracted_as_a_plain_tuple_of_names(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [_pr(7, labels=("security", "founder-review"))],
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert report[0].labels == ("security", "founder-review")


@pytest.mark.asyncio
async def test_open_issues_are_included_and_never_a_pr_mixed_in(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [],
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [
                {
                    "number": 10,
                    "title": "a real issue",
                    "html_url": "https://github.com/d1n095/LifeAI/issues/10",
                    "updated_at": "2026-11-13T00:00:00Z",
                    "labels": [],
                },
                {
                    "number": 11,
                    "title": "a PR, mixed in by GitHub's own API",
                    "html_url": "https://github.com/d1n095/LifeAI/issues/11",
                    "updated_at": "2026-11-13T00:00:00Z",
                    "pull_request": {"url": "..."},
                    "labels": [],
                },
            ],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, now=FIXED_NOW)

    assert len(report) == 1
    assert report[0].kind == "open_issue"
    assert report[0].number == 10


@pytest.mark.asyncio
async def test_old_issue_is_flagged_stale(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls?state=open&per_page=25&page=1"): [],
            ("GET", "/repos/d1n095/LifeAI/issues?state=open&per_page=30&page=1"): [
                {
                    "number": 12,
                    "title": "stale issue",
                    "html_url": "https://github.com/d1n095/LifeAI/issues/12",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "labels": [],
                }
            ],
        },
    )
    client = _configured_client(monkeypatch)

    report = await build_github_work_queue_report(client, stale_days=7, now=FIXED_NOW)

    assert "stale_7d" in report[0].signals
