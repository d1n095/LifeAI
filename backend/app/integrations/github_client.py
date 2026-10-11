"""Minimal GitHub REST client for MainAI Core's agent orchestration (see
app/agent_orchestration.py) and, as of the MainAI Execution Loop V0.1 executor
(app/mainai_execution/execution_job.py), real repo edits.

  - read: get_ref, get_pull_request, list_check_runs (paginated), list_branches,
    list_open_pull_requests, list_pull_request_comments, get_file_contents
  - write (only ever called when settings.github_write_enabled is True — see
    prepare_github_pr()): create_branch, create_or_update_file, create_pull_request,
    update_pull_request
  - write, multi-file (Git Data API): create_blob, get_commit, create_tree, create_commit,
    update_ref, and the orchestrating commit_multiple_files() — see that method's own
    docstring for why this exists: app/agent_orchestration.py's create_or_update_file() path
    was a deliberate, self-acknowledged stub (one PUT /contents call per file, no real
    multi-file atomicity, a diff written as a single artifact file rather than applied to the
    tree). commit_multiple_files() is the real capability that closes that gap — an actual
    multi-file tree/commit built from real file contents, landing as ONE atomic commit
    (either every file's blob lands in the new tree and the branch ref advances, or none of
    it does — GitHub's Git Data API commit+ref-update is the atomicity boundary, not a
    client-side transaction this module invents).

There is NO merge method anywhere in this module, on purpose — not "implemented but gated
behind a flag", genuinely absent. CLAUDE.md's 2026-07-26 MainAI Core direction requires auto-
merge to sit behind a disabled feature flag AND an approval gate AND a proven track record;
until a real merge implementation is deliberately added here in a future, separately-reviewed
change, `github_auto_merge_enabled` has no code path to gate at all — attempt_auto_merge() in
app/agent_orchestration.py always reports "blocked" for exactly this reason.

Also deliberately absent: force-push, branch deletion, and any deploy/governance operation —
none of those have a use case in agent orchestration and CLAUDE.md prohibits all of them
without a separate, explicit human decision regardless.
"""

import base64
from urllib.parse import quote

import httpx

from app.config import get_settings

API_BASE = "https://api.github.com"
_MAX_PAGES = 50


class GitHubClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.github_token and self.settings.github_repo)

    def _require_configured(self) -> str:
        if not self.is_configured():
            raise GitHubClientError(
                "GITHUB_TOKEN och/eller GITHUB_REPO är inte satta — MainAI kan inte prata med GitHub."
            )
        return self.settings.github_repo

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float = 30) -> dict:
        resp = await self._request_response(method, path, json=json, timeout=timeout)
        return resp.json() if resp.content else {}

    async def _request_response(
        self, method: str, path: str, *, json: dict | None = None, timeout: float = 30
    ) -> httpx.Response:
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=self._headers(), json=json)
            if resp.status_code >= 400:
                raise GitHubClientError(
                    f"GitHub {method} {path} misslyckades ({resp.status_code}): {resp.text[:500]}",
                    status_code=resp.status_code,
                )
            return resp

    def _next_page_url(self, resp: httpx.Response) -> str | None:
        next_link = resp.links.get("next") if resp.links else None
        if not next_link:
            return None
        return next_link.get("url")

    async def _get_paginated(self, path: str) -> list:
        """Follow GitHub Link rel=next until exhausted. Each page must be a JSON array."""
        items: list = []
        current = path
        for _ in range(_MAX_PAGES):
            resp = await self._request_response("GET", current)
            payload = resp.json() if resp.content else []
            if payload is None:
                payload = []
            if not isinstance(payload, list):
                raise GitHubClientError(f"GitHub GET {current} returned a non-list page")
            items.extend(payload)
            nxt = self._next_page_url(resp)
            if not nxt:
                return items
            current = nxt
        raise GitHubClientError(f"GitHub GET {path} exceeded {_MAX_PAGES} pages")

    async def _get_paginated_key(self, path: str, key: str) -> list:
        """Follow Link rel=next for object pages that nest the list under `key`."""
        items: list = []
        current = path
        for _ in range(_MAX_PAGES):
            resp = await self._request_response("GET", current)
            payload = resp.json() if resp.content else {}
            if not isinstance(payload, dict):
                raise GitHubClientError(f"GitHub GET {current} returned a non-object page")
            page = payload.get(key) or []
            if not isinstance(page, list):
                raise GitHubClientError(f"GitHub GET {current} missing list field {key!r}")
            items.extend(page)
            nxt = self._next_page_url(resp)
            if not nxt:
                return items
            current = nxt
        raise GitHubClientError(f"GitHub GET {path} exceeded {_MAX_PAGES} pages")

    # --- Read ---------------------------------------------------------------------------

    async def get_ref(self, branch: str) -> str:
        """Returns the commit SHA a branch currently points at."""
        repo = self._require_configured()
        data = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def get_pull_request(self, number: int) -> dict:
        repo = self._require_configured()
        return await self._request("GET", f"/repos/{repo}/pulls/{number}")

    async def list_pull_requests_for_head(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        """Hardening pass finding (P1): the resume-safety counterpart to `get_ref(branch)` in
        commit_multiple_files()'s own crash-window fix -- lets a caller check "does a PR for
        this exact head/base already exist" BEFORE calling create_pull_request(), so a crash
        between a successful PR creation and its durable checkpoint doesn't create a SECOND,
        duplicate PR on resume (see app/mainai_execution/execution_job.py's `_handle_open_pr()`).
        `head` must be in GitHub's own `owner:branch` filter format -- see that caller for how
        it's constructed; passing a bare branch name silently matches nothing, not an error."""
        repo = self._require_configured()
        return await self._request("GET", f"/repos/{repo}/pulls?head={head}&base={base}&state={state}")

    async def list_check_runs(self, ref: str) -> list[dict]:
        """All check runs for `ref`, following GitHub pagination. Same public signature."""
        repo = self._require_configured()
        encoded_ref = quote(ref, safe="")
        return await self._get_paginated_key(
            f"/repos/{repo}/commits/{encoded_ref}/check-runs?per_page=100",
            "check_runs",
        )

    async def list_branches(self, *, prefix: str = "") -> list[dict]:
        """Read-only branch list. When `prefix` is set, only names that start with it remain."""
        repo = self._require_configured()
        rows = await self._get_paginated(f"/repos/{repo}/branches?per_page=100")
        if prefix:
            rows = [row for row in rows if str(row.get("name") or "").startswith(prefix)]
        return rows

    async def list_open_pull_requests(self) -> list[dict]:
        """Read-only open PRs for the configured repo, following pagination."""
        repo = self._require_configured()
        return await self._get_paginated(f"/repos/{repo}/pulls?state=open&per_page=100")

    async def list_pull_request_comments(self, number: int) -> list[dict]:
        """Read-only conversation comments on a PR (GitHub issue-comment API)."""
        repo = self._require_configured()
        return await self._get_paginated(f"/repos/{repo}/issues/{int(number)}/comments?per_page=100")

    async def get_file_contents(self, path: str, *, ref: str) -> str | None:
        """Decoded UTF-8 file text at `ref`. Missing path returns None; directories raise."""
        repo = self._require_configured()
        encoded_path = quote(path, safe="/")
        encoded_ref = quote(ref, safe="")
        try:
            data = await self._request("GET", f"/repos/{repo}/contents/{encoded_path}?ref={encoded_ref}")
        except GitHubClientError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubClientError(f"GitHub contents at {path!r} is not a file")
        raw = data.get("content")
        if not raw:
            return ""
        try:
            return base64.b64decode("".join(str(raw).split()), validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubClientError(f"GitHub file {path!r} is not valid UTF-8 text") from exc

    # --- Write (only called when settings.github_write_enabled is True) -----------------

    async def create_branch(self, *, new_branch: str, from_sha: str) -> dict:
        repo = self._require_configured()
        return await self._request(
            "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{new_branch}", "sha": from_sha}
        )

    async def create_or_update_file(self, *, path: str, content: str, message: str, branch: str, sha: str | None = None) -> dict:
        """`sha` is required by GitHub's API when updating an existing file, omitted when
        creating a new one — same semantics as the GitHub Contents API itself."""
        repo = self._require_configured()
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return await self._request("PUT", f"/repos/{repo}/contents/{path}", json=payload)

    async def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> dict:
        repo = self._require_configured()
        return await self._request("POST", f"/repos/{repo}/pulls", json={"title": title, "body": body, "head": head, "base": base})

    async def update_pull_request(self, number: int, *, title: str | None = None, body: str | None = None) -> dict:
        repo = self._require_configured()
        payload = {k: v for k, v in {"title": title, "body": body}.items() if v is not None}
        return await self._request("PATCH", f"/repos/{repo}/pulls/{number}", json=payload)

    # --- Write, multi-file (Git Data API) — the real repo-edit capability ---------------

    async def get_commit(self, sha: str) -> dict:
        """Returns a commit object, including `tree.sha` — the base tree
        commit_multiple_files() builds the new tree on top of."""
        repo = self._require_configured()
        return await self._request("GET", f"/repos/{repo}/git/commits/{sha}")

    async def create_blob(self, *, content: str, encoding: str = "utf-8") -> str:
        """Returns the new blob's SHA. `encoding` is GitHub's own accepted values
        (`utf-8` or `base64`) — content is passed through as-is, never re-encoded here,
        so a caller with binary content can pass `encoding="base64"` and pre-encoded
        `content` directly."""
        repo = self._require_configured()
        data = await self._request("POST", f"/repos/{repo}/git/blobs", json={"content": content, "encoding": encoding})
        return data["sha"]

    async def create_tree(self, *, base_tree: str, entries: list[dict]) -> str:
        """`entries`: `[{"path": ..., "mode": "100644", "type": "blob", "sha": <blob sha>}]`.
        Returns the new tree's SHA. GitHub's tree API only needs the CHANGED entries plus
        `base_tree` — every other file already in `base_tree` is carried forward untouched,
        so this is inherently a partial, additive write, never a full-tree reconstruction."""
        repo = self._require_configured()
        data = await self._request("POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": entries})
        return data["sha"]

    async def create_commit(self, *, message: str, tree: str, parents: list[str]) -> str:
        """Returns the new commit's SHA."""
        repo = self._require_configured()
        data = await self._request("POST", f"/repos/{repo}/git/commits", json={"message": message, "tree": tree, "parents": parents})
        return data["sha"]

    async def update_ref(self, *, branch: str, sha: str, force: bool = False) -> dict:
        """Advances `branch` to point at `sha`. `force` defaults False (fast-forward only) —
        a non-fast-forward update means something else moved the branch since this commit's
        parent was read, and that race must surface as an error, never be silently
        overwritten (see commit_multiple_files())."""
        repo = self._require_configured()
        return await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha, "force": force})

    async def commit_multiple_files(self, *, branch: str, base_sha: str, files: list[dict], message: str) -> dict:
        """The real multi-file apply/commit capability (see this module's own docstring for
        the stub this replaces). `files`: `[{"path": "backend/app/x.py", "content": "...new
        full file content..."}]` — content is the file's complete new contents, not a diff;
        the executor (app/mainai_execution/execution_job.py) is responsible for deciding what
        the new content should be (e.g. asking the code-agent provider for full replacement
        content for a small, explicitly-scoped set of files, never an unbounded rewrite).

        Steps, each a real GitHub API call, landing as ONE atomic commit:
          1. get_commit(base_sha) -> base_tree sha (the tree `branch` pointed at when this
             task was dispatched, NOT re-read at call time -- see the race note below).
          2. create_blob() for each file's new content.
          3. create_tree(base_tree, entries) -- one new tree with exactly the changed paths.
          4. create_commit(tree, parents=[base_sha]).
          5. update_ref(branch, new commit sha, force=False) -- fast-forward only.

        Returns `{"commit_sha": ..., "tree_sha": ..., "blob_shas": {path: sha}}` so the
        caller can record exact provenance (never just "a commit was made somewhere").

        Race safety: step 5's fast-forward-only update_ref() is the actual safety boundary —
        if `branch` moved to a different commit between this task being dispatched (when
        `base_sha` was captured) and this call, GitHub rejects the non-fast-forward update
        and raises GitHubClientError rather than silently overwriting or rebasing. The caller
        must treat that as a genuine execution failure (retryable: re-read the branch, redo
        the edit against the new base), never retry blindly with `force=True`."""
        base_commit = await self.get_commit(base_sha)
        base_tree_sha = base_commit["tree"]["sha"]

        blob_shas: dict[str, str] = {}
        for file in files:
            blob_shas[file["path"]] = await self.create_blob(content=file["content"])

        tree_entries = [{"path": path, "mode": "100644", "type": "blob", "sha": sha} for path, sha in blob_shas.items()]
        new_tree_sha = await self.create_tree(base_tree=base_tree_sha, entries=tree_entries)

        new_commit_sha = await self.create_commit(message=message, tree=new_tree_sha, parents=[base_sha])

        await self.update_ref(branch=branch, sha=new_commit_sha, force=False)

        return {"commit_sha": new_commit_sha, "tree_sha": new_tree_sha, "blob_shas": blob_shas}


def get_github_client() -> GitHubClient:
    return GitHubClient()
