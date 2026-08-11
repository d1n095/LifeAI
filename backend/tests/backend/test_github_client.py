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
