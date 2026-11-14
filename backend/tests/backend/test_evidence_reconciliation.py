"""app/mainai_execution/evidence_reconciliation.py — Continuous Build Directive, Section D:
"never turn a PR description into verified execution evidence." Same httpx-mocking technique
as test_github_client.py (real httpx.Response objects, network never touched) so these tests
exercise the real GitHubClient call path, not a mocked reconcile_pull_request_status()."""

import httpx
import pytest

from app.integrations.github_client import GitHubClient
from app.mainai_execution.evidence_reconciliation import reconcile_pull_request_status


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


def _pr(*, commits=0, merged=False, merged_at=None, head_sha="sha1"):
    return {"number": 1, "commits": commits, "merged": merged, "merged_at": merged_at, "head": {"sha": head_sha}}


@pytest.mark.asyncio
async def test_a_pr_shell_with_no_commits_is_only_proposed(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=0),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.status == "proposed"
    assert result.implemented is False
    assert result.tested is False
    assert result.reviewed is False
    assert result.integrated is False
    assert result.activated is False


@pytest.mark.asyncio
async def test_commits_pushed_with_no_ci_yet_is_implemented(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {"check_runs": []},
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.implemented is True
    assert result.status == "implemented"


@pytest.mark.asyncio
async def test_a_single_failing_check_run_among_passing_ones_is_not_tested(monkeypatch):
    """The exact standard this session's own CI babysitting already holds itself to: one red
    check among several green ones is NOT "tested", even if most checks pass."""
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "completed", "conclusion": "failure"},
                ]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.tested is False
    assert result.status == "implemented"


@pytest.mark.asyncio
async def test_all_checks_green_is_tested(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.tested is True
    assert result.status == "tested"


@pytest.mark.asyncio
async def test_a_comment_only_review_does_not_count_as_reviewed(monkeypatch):
    """A review that merely COMMENTED (no APPROVED state) must never be read as an approval --
    this is exactly the "don't trust the text" standard applied to review state too."""
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [
                {"state": "COMMENTED", "user": {"login": "someone"}}
            ],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.reviewed is False
    assert result.status == "tested"


@pytest.mark.asyncio
async def test_an_approved_review_reaches_reviewed(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [
                {"state": "APPROVED", "user": {"login": "founder"}}
            ],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.reviewed is True
    assert result.status == "reviewed"


@pytest.mark.asyncio
async def test_a_merged_pr_reaches_integrated_regardless_of_mergeable_state_text(monkeypatch):
    """integrated must come from GitHub's own `merged` field, never a mergeable_state
    heuristic (which only describes whether a merge is POSSIBLE, not whether one happened)."""
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3, merged=True, merged_at="2026-11-14T00:00:00Z"),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [
                {"state": "APPROVED", "user": {"login": "founder"}}
            ],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.integrated is True
    assert result.status == "integrated"


@pytest.mark.asyncio
async def test_activated_is_never_reported_true_even_when_everything_else_is(monkeypatch):
    """The explicit, honest limitation: no activation-evidence source exists in this codebase
    yet, so this function must never fabricate one -- matching Directive A's "UNKNOWN/BLOCKED,
    never PASS" and this directive's own Item F ("record that limitation... instead of
    simulating")."""
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/pulls/1"): _pr(commits=3, merged=True, merged_at="2026-11-14T00:00:00Z"),
            ("GET", "/repos/d1n095/LifeAI/commits/sha1/check-runs"): {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            ("GET", "/repos/d1n095/LifeAI/pulls/1/reviews?per_page=30&page=1"): [
                {"state": "APPROVED", "user": {"login": "founder"}}
            ],
        },
    )
    client = _configured_client(monkeypatch)

    result = await reconcile_pull_request_status(client, number=1)

    assert result.activated is False
    assert result.status == "integrated"
    assert "no activation-evidence source" in result.evidence["activated"]
