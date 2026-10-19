"""app/integrations/github_client.py — direct unit coverage at the httpx call boundary.
Previously this module had NO dedicated test file at all (only exercised indirectly, with the
client itself monkeypatched, via tests/backend/test_agent_orchestration.py) — confirmed during
the MainAI Execution Loop V0.1 architecture research. This file closes that gap, with primary
focus on commit_multiple_files() (the Git Data API multi-file commit that replaces
app/agent_orchestration.py's single-file-artifact stub — see that method's own docstring).

Real httpx.Response objects, network never actually touched (monkeypatches
httpx.AsyncClient.request, same technique tests/backend/providers/test_gemini_provider.py
already establishes for httpx.AsyncClient.post)."""

import httpx
import pytest

from app.integrations.github_client import GitHubClient, GitHubClientError


def _fake_github(monkeypatch, responses: dict[tuple[str, str], dict | list]):
    """`responses`: {(method, path): json_body}. Records every call for assertion and returns
    the matching fake response, or a 404 if the (method, path) pair wasn't expected."""
    calls = []

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        path = str(url).removeprefix("https://api.github.com")
        calls.append({"method": method, "path": path, "headers": headers, "json": json})
        request = httpx.Request(method, url)
        key = (method, path)
        if key not in responses:
            return httpx.Response(404, request=request, json={"message": f"unexpected call: {method} {path}"})
        return httpx.Response(200, request=request, json=responses[key])

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    return calls


def _configured_client(monkeypatch) -> GitHubClient:
    client = GitHubClient()
    monkeypatch.setattr(client.settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(client.settings, "github_repo", "d1n095/LifeAI")
    return client


@pytest.mark.asyncio
async def test_is_configured_false_without_token_or_repo():
    client = GitHubClient()
    assert client.is_configured() is False


@pytest.mark.asyncio
async def test_unconfigured_client_raises_before_any_network_call(monkeypatch):
    calls = _fake_github(monkeypatch, {})
    client = GitHubClient()
    with pytest.raises(GitHubClientError):
        await client.get_ref("main")
    assert calls == []


@pytest.mark.asyncio
async def test_get_commit_returns_tree_sha(monkeypatch):
    _fake_github(monkeypatch, {("GET", "/repos/d1n095/LifeAI/git/commits/base123"): {"sha": "base123", "tree": {"sha": "tree456"}}})
    client = _configured_client(monkeypatch)

    commit = await client.get_commit("base123")
    assert commit["tree"]["sha"] == "tree456"


@pytest.mark.asyncio
async def test_create_blob_returns_sha(monkeypatch):
    _fake_github(monkeypatch, {("POST", "/repos/d1n095/LifeAI/git/blobs"): {"sha": "blobabc"}})
    client = _configured_client(monkeypatch)

    sha = await client.create_blob(content="hello world")
    assert sha == "blobabc"


@pytest.mark.asyncio
async def test_commit_multiple_files_makes_exactly_the_expected_sequence_of_calls(monkeypatch):
    calls = _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/git/commits/base123"): {"sha": "base123", "tree": {"sha": "tree456"}},
            ("POST", "/repos/d1n095/LifeAI/git/blobs"): {"sha": "blob-one"},
            ("POST", "/repos/d1n095/LifeAI/git/trees"): {"sha": "newtree789"},
            ("POST", "/repos/d1n095/LifeAI/git/commits"): {"sha": "newcommitxyz"},
            ("PATCH", "/repos/d1n095/LifeAI/git/refs/heads/claude/demo-branch"): {"ref": "refs/heads/claude/demo-branch"},
        },
    )
    client = _configured_client(monkeypatch)

    result = await client.commit_multiple_files(
        branch="claude/demo-branch",
        base_sha="base123",
        files=[{"path": "docs/EXAMPLE.md", "content": "# Example\n\nFixed a stale reference.\n"}],
        message="Fix a stale doc reference",
    )

    assert result == {"commit_sha": "newcommitxyz", "tree_sha": "newtree789", "blob_shas": {"docs/EXAMPLE.md": "blob-one"}}

    methods_and_paths = [(c["method"], c["path"]) for c in calls]
    assert methods_and_paths == [
        ("GET", "/repos/d1n095/LifeAI/git/commits/base123"),
        ("POST", "/repos/d1n095/LifeAI/git/blobs"),
        ("POST", "/repos/d1n095/LifeAI/git/trees"),
        ("POST", "/repos/d1n095/LifeAI/git/commits"),
        ("PATCH", "/repos/d1n095/LifeAI/git/refs/heads/claude/demo-branch"),
    ]

    # The tree call must build on the REAL base_tree sha, and include exactly the changed path.
    tree_call = next(c for c in calls if c["path"] == "/repos/d1n095/LifeAI/git/trees")
    assert tree_call["json"]["base_tree"] == "tree456"
    assert tree_call["json"]["tree"] == [{"path": "docs/EXAMPLE.md", "mode": "100644", "type": "blob", "sha": "blob-one"}]

    # The commit's only parent must be base_sha -- never an unrelated/empty parents list.
    commit_call = next(c for c in calls if c["path"] == "/repos/d1n095/LifeAI/git/commits")
    assert commit_call["json"]["parents"] == ["base123"]

    # The ref update must be fast-forward only (force=False) -- see commit_multiple_files()'s
    # own docstring on why a non-fast-forward race must surface as an error, never be silently
    # overwritten.
    ref_call = next(c for c in calls if "refs/heads" in c["path"])
    assert ref_call["json"]["force"] is False
    assert ref_call["json"]["sha"] == "newcommitxyz"


@pytest.mark.asyncio
async def test_commit_multiple_files_with_several_files_creates_one_blob_per_file(monkeypatch):
    blob_call_count = 0

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        nonlocal blob_call_count
        path = str(url).removeprefix("https://api.github.com")
        request = httpx.Request(method, url)
        if path == "/repos/d1n095/LifeAI/git/commits/base123":
            return httpx.Response(200, request=request, json={"sha": "base123", "tree": {"sha": "tree456"}})
        if path == "/repos/d1n095/LifeAI/git/blobs":
            blob_call_count += 1
            return httpx.Response(200, request=request, json={"sha": f"blob-{blob_call_count}"})
        if path == "/repos/d1n095/LifeAI/git/trees":
            return httpx.Response(200, request=request, json={"sha": "newtree"})
        if path == "/repos/d1n095/LifeAI/git/commits":
            return httpx.Response(200, request=request, json={"sha": "newcommit"})
        if "refs/heads" in path:
            return httpx.Response(200, request=request, json={"ref": path})
        return httpx.Response(404, request=request, json={"message": "unexpected"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)

    result = await client.commit_multiple_files(
        branch="claude/demo-branch",
        base_sha="base123",
        files=[
            {"path": "a.py", "content": "print('a')\n"},
            {"path": "b.py", "content": "print('b')\n"},
            {"path": "c.py", "content": "print('c')\n"},
        ],
        message="three-file change",
    )

    assert blob_call_count == 3
    assert set(result["blob_shas"].keys()) == {"a.py", "b.py", "c.py"}


@pytest.mark.asyncio
async def test_commit_multiple_files_raises_on_non_fast_forward_ref_update_race(monkeypatch):
    """The branch moved (a concurrent push/merge) since base_sha was captured -- GitHub
    rejects the fast-forward-only ref update. commit_multiple_files() must surface this as a
    real GitHubClientError, never silently succeed or retry with force."""

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        path = str(url).removeprefix("https://api.github.com")
        request = httpx.Request(method, url)
        if path == "/repos/d1n095/LifeAI/git/commits/base123":
            return httpx.Response(200, request=request, json={"sha": "base123", "tree": {"sha": "tree456"}})
        if path == "/repos/d1n095/LifeAI/git/blobs":
            return httpx.Response(200, request=request, json={"sha": "blob-1"})
        if path == "/repos/d1n095/LifeAI/git/trees":
            return httpx.Response(200, request=request, json={"sha": "newtree"})
        if path == "/repos/d1n095/LifeAI/git/commits":
            return httpx.Response(200, request=request, json={"sha": "newcommit"})
        if "refs/heads" in path:
            return httpx.Response(422, request=request, json={"message": "Update is not a fast forward"})
        return httpx.Response(404, request=request, json={"message": "unexpected"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)

    with pytest.raises(GitHubClientError, match="fast forward"):
        await client.commit_multiple_files(
            branch="claude/demo-branch", base_sha="base123", files=[{"path": "a.py", "content": "x"}], message="msg"
        )


@pytest.mark.asyncio
async def test_create_pull_request_can_open_a_draft(monkeypatch):
    calls = _fake_github(monkeypatch, {("POST", "/repos/d1n095/LifeAI/pulls"): {"number": 9, "draft": True}})
    client = _configured_client(monkeypatch)
    pr = await client.create_pull_request(title="t", body="b", head="feat", base="main", draft=True)
    assert pr["number"] == 9
    assert calls[0]["json"]["draft"] is True


@pytest.mark.asyncio
async def test_create_pull_request_defaults_to_not_draft(monkeypatch):
    calls = _fake_github(monkeypatch, {("POST", "/repos/d1n095/LifeAI/pulls"): {"number": 10, "draft": False}})
    client = _configured_client(monkeypatch)
    await client.create_pull_request(title="t", body="b", head="feat", base="main")
    assert calls[0]["json"]["draft"] is False


def _link_next(url: str) -> str:
    return f'<{url}>; rel="next"'


@pytest.mark.asyncio
async def test_list_check_runs_follows_pagination(monkeypatch):
    calls = []

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        calls.append({"method": method, "url": str(url), "json": json})
        request = httpx.Request(method, url)
        path = str(url)
        if path.endswith("/check-runs?per_page=100"):
            return httpx.Response(
                200,
                request=request,
                headers={"Link": _link_next("https://api.github.com/repos/d1n095/LifeAI/commits/abc/check-runs?page=2")},
                json={"total_count": 3, "check_runs": [{"id": 1, "name": "lint"}, {"id": 2, "name": "test"}]},
            )
        if "check-runs?page=2" in path:
            return httpx.Response(200, request=request, json={"total_count": 3, "check_runs": [{"id": 3, "name": "e2e"}]})
        return httpx.Response(404, request=request, json={"message": f"unexpected {path}"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)

    runs = await client.list_check_runs("abc")
    assert [run["id"] for run in runs] == [1, 2, 3]
    assert all(call["method"] == "GET" for call in calls)
    assert all(call["json"] is None for call in calls)


@pytest.mark.asyncio
async def test_list_check_runs_missing_payload_is_empty(monkeypatch):
    _fake_github(monkeypatch, {("GET", "/repos/d1n095/LifeAI/commits/abc/check-runs?per_page=100"): {}})
    client = _configured_client(monkeypatch)
    assert await client.list_check_runs("abc") == []


@pytest.mark.asyncio
async def test_list_check_runs_raises_on_github_failure(monkeypatch):
    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(502, request=request, json={"message": "bad gateway"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)
    with pytest.raises(GitHubClientError, match="502"):
        await client.list_check_runs("abc")


@pytest.mark.asyncio
async def test_list_branches_filters_by_prefix(monkeypatch):
    _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/branches?per_page=100"): [
                {"name": "cursor/mainai-github-readonly-discovery-91c1"},
                {"name": "cursor/other"},
                {"name": "claude/det-kommer-mer-879lcm"},
            ]
        },
    )
    client = _configured_client(monkeypatch)
    rows = await client.list_branches(prefix="cursor/")
    assert [row["name"] for row in rows] == [
        "cursor/mainai-github-readonly-discovery-91c1",
        "cursor/other",
    ]


@pytest.mark.asyncio
async def test_list_open_pull_requests_paginates_and_is_get_only(monkeypatch):
    calls = []

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        calls.append({"method": method, "url": str(url), "json": json})
        request = httpx.Request(method, url)
        if str(url).endswith("/pulls?state=open&per_page=100"):
            return httpx.Response(
                200,
                request=request,
                headers={"Link": _link_next("https://api.github.com/repos/d1n095/LifeAI/pulls?state=open&page=2")},
                json=[{"number": 1, "title": "first"}],
            )
        if "pulls?state=open&page=2" in str(url):
            return httpx.Response(200, request=request, json=[{"number": 2, "title": "second"}])
        return httpx.Response(404, request=request, json={"message": "unexpected"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)
    prs = await client.list_open_pull_requests()
    assert [pr["number"] for pr in prs] == [1, 2]
    assert {call["method"] for call in calls} == {"GET"}
    assert all(call["json"] is None for call in calls)


@pytest.mark.asyncio
async def test_list_pull_request_comments_and_file_contents(monkeypatch):
    import base64

    encoded = base64.b64encode(b"hello discovery\n").decode("ascii")
    calls = _fake_github(
        monkeypatch,
        {
            ("GET", "/repos/d1n095/LifeAI/issues/12/comments?per_page=100"): [{"id": 9, "body": "please examine"}],
            ("GET", "/repos/d1n095/LifeAI/contents/backend/app/integrations/github_client.py?ref=abc123"): {
                "type": "file",
                "encoding": "base64",
                "content": encoded,
            },
        },
    )
    client = _configured_client(monkeypatch)
    comments = await client.list_pull_request_comments(12)
    assert comments[0]["body"] == "please examine"
    text = await client.get_file_contents("backend/app/integrations/github_client.py", ref="abc123")
    assert text == "hello discovery\n"
    assert all(call["method"] == "GET" for call in calls)
    assert all(call["json"] is None for call in calls)


@pytest.mark.asyncio
async def test_get_file_contents_missing_path_is_none(monkeypatch):
    _fake_github(monkeypatch, {})
    client = _configured_client(monkeypatch)
    assert await client.get_file_contents("docs/NOPE.md", ref="abc123") is None


@pytest.mark.asyncio
async def test_get_file_contents_raises_on_github_failure(monkeypatch):
    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(500, request=request, json={"message": "boom"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)
    with pytest.raises(GitHubClientError, match="500"):
        await client.get_file_contents("README.md", ref="abc123")


@pytest.mark.asyncio
async def test_discovery_methods_do_not_issue_writes(monkeypatch):
    writes = []

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        if method != "GET":
            writes.append(method)
        request = httpx.Request(method, url)
        path = str(url).removeprefix("https://api.github.com")
        if path.startswith("/repos/d1n095/LifeAI/branches"):
            return httpx.Response(200, request=request, json=[])
        if path.startswith("/repos/d1n095/LifeAI/pulls"):
            return httpx.Response(200, request=request, json=[])
        if "/comments" in path:
            return httpx.Response(200, request=request, json=[])
        if "/contents/" in path:
            return httpx.Response(404, request=request, json={"message": "Not Found"})
        if "/check-runs" in path:
            return httpx.Response(200, request=request, json={"check_runs": []})
        return httpx.Response(404, request=request, json={"message": "unexpected"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)
    await client.list_branches(prefix="cursor/")
    await client.list_open_pull_requests()
    await client.list_pull_request_comments(1)
    await client.get_file_contents("x.py", ref="sha")
    await client.list_check_runs("sha")
    assert writes == []



@pytest.mark.asyncio
async def test_list_workflow_runs_for_sha_is_read_only_and_paginated(monkeypatch):
    calls = []

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        calls.append({"method": method, "url": str(url), "json": json})
        request = httpx.Request(method, url)
        path = str(url)
        if path.endswith("/actions/runs?head_sha=abc&per_page=100"):
            nxt = "https://api.github.com/repos/d1n095/LifeAI/actions/runs?head_sha=abc&per_page=100&page=2"
            return httpx.Response(200, request=request, headers={"Link": f'<{nxt}>; rel="next"'},
                                  json={"total_count": 2, "workflow_runs": [{"id": 1, "event": "push", "check_suite_id": 11}]})
        if "page=2" in path:
            return httpx.Response(200, request=request, json={"total_count": 2, "workflow_runs": [{"id": 2, "event": "pull_request", "check_suite_id": 12}]})
        return httpx.Response(404, request=request, json={"message": f"unexpected {path}"})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    client = _configured_client(monkeypatch)
    runs = await client.list_workflow_runs_for_sha("abc")
    assert [r["event"] for r in runs] == ["push", "pull_request"]
    assert all(c["method"] == "GET" and c["json"] is None for c in calls)
