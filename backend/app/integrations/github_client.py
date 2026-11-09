"""Minimal GitHub REST client for MainAI Core's agent orchestration (see
app/agent_orchestration.py) and, as of the MainAI Execution Loop V0.1 executor
(app/mainai_execution/execution_job.py), real repo edits.

  - read: refs, PRs/reviews/comments, commit/check metadata, Actions runs/jobs, and bounded
    pagination for the read-only GitHub Operations Center.
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
import asyncio
import re
from dataclasses import dataclass
from time import time
from urllib.parse import quote, urlencode

import httpx

from app.config import get_settings

API_BASE = "https://api.github.com"


class GitHubClientError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubClientError):
    def __init__(self, status_code: int, retry_after_seconds: float | None):
        super().__init__(f"GitHub read rate limited ({status_code}).")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class GitHubListResult:
    items: list[dict]
    pages_fetched: int
    truncated: bool


class GitHubClient:
    READ_TIMEOUT_SECONDS = 8.0
    MAX_READ_ATTEMPTS = 3
    MAX_PAGES = 5
    PAGE_SIZE = 100

    def __init__(self):
        self.settings = get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.github_token and self.settings.github_repo)

    def _require_configured(self) -> str:
        if not self.is_configured():
            raise GitHubClientError(
                "GITHUB_TOKEN och/eller GITHUB_REPO är inte satta — MainAI kan inte prata med GitHub."
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.settings.github_repo or ""):
            raise GitHubClientError("GITHUB_REPO must identify one owner/repository.")
        return self.settings.github_repo

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float = 30) -> dict | list:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{API_BASE}{path}", headers=self._headers(), json=json)
            if resp.status_code >= 400:
                if resp.status_code == 429 or (
                    resp.status_code == 403 and "rate limit" in resp.text.lower()
                ):
                    retry_after = resp.headers.get("Retry-After")
                    reset_at = resp.headers.get("X-RateLimit-Reset")
                    try:
                        retry_after_seconds = float(retry_after) if retry_after else None
                    except ValueError:
                        retry_after_seconds = None
                    if retry_after_seconds is None and reset_at:
                        try:
                            retry_after_seconds = max(0.0, float(reset_at) - time())
                        except ValueError:
                            retry_after_seconds = None
                    raise GitHubRateLimitError(resp.status_code, retry_after_seconds)
                raise GitHubClientError(f"GitHub {method} {path} misslyckades ({resp.status_code}): {resp.text[:500]}")
            return resp.json() if resp.content else {}

    # --- Read ---------------------------------------------------------------------------

    async def _read_request(self, path: str) -> dict | list:
        """Bounded retry for safe GETs only; mutations are never retried automatically."""
        for attempt in range(self.MAX_READ_ATTEMPTS):
            try:
                return await self._request("GET", path, timeout=self.READ_TIMEOUT_SECONDS)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 == self.MAX_READ_ATTEMPTS:
                    raise GitHubClientError("GitHub read timed out or the network was unavailable.") from exc
            except GitHubRateLimitError as exc:
                if attempt + 1 == self.MAX_READ_ATTEMPTS or (
                    exc.retry_after_seconds is not None and exc.retry_after_seconds > 2
                ):
                    raise GitHubClientError("GitHub read is rate limited; refresh later.") from exc
                delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else 0.1 * (attempt + 1)
                await asyncio.sleep(max(0.0, delay))
                continue
            except GitHubClientError as exc:
                match = re.search(r"\((\d{3})\)", str(exc))
                status_code = int(match.group(1)) if match else None
                retryable = status_code == 429 or status_code is not None and status_code >= 500
                if status_code == 403 and "rate limit" in str(exc).lower():
                    retryable = True
                if not retryable or attempt + 1 == self.MAX_READ_ATTEMPTS:
                    raise
            await asyncio.sleep(0.1 * (attempt + 1))
        raise GitHubClientError("GitHub read failed.")

    async def _list_paginated(
        self,
        path: str,
        *,
        params: dict[str, str | int],
        item_key: str | None = None,
        max_pages: int = MAX_PAGES,
        page_size: int = PAGE_SIZE,
    ) -> GitHubListResult:
        if not 1 <= max_pages <= self.MAX_PAGES or not 1 <= page_size <= self.PAGE_SIZE:
            raise ValueError("GitHub pagination limits are outside the configured bounds.")
        items: list[dict] = []
        pages_fetched = 0
        truncated = False
        for page in range(1, max_pages + 1):
            query = urlencode({**params, "per_page": page_size, "page": page})
            data = await self._read_request(f"{path}?{query}")
            page_items = data.get(item_key, []) if item_key and isinstance(data, dict) else data
            if not isinstance(page_items, list):
                raise GitHubClientError("GitHub returned an unexpected list response.")
            pages_fetched += 1
            items.extend(item for item in page_items if isinstance(item, dict))
            if len(page_items) < page_size:
                break
            if page == max_pages:
                truncated = True
        return GitHubListResult(items=items, pages_fetched=pages_fetched, truncated=truncated)

    async def get_repository(self) -> dict:
        repo = self._require_configured()
        return await self._read_request(f"/repos/{repo}")

    async def get_ref(self, branch: str) -> str:
        """Returns the commit SHA a branch currently points at."""
        repo = self._require_configured()
        data = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def get_pull_request(self, number: int) -> dict:
        repo = self._require_configured()
        return await self._read_request(f"/repos/{repo}/pulls/{number}")

    async def list_pull_requests(
        self, *, state: str, max_pages: int = MAX_PAGES, page_size: int = PAGE_SIZE
    ) -> GitHubListResult:
        if state not in {"open", "closed"}:
            raise ValueError("Pull request state must be open or closed.")
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/pulls",
            params={"state": state, "sort": "updated", "direction": "desc"},
            max_pages=max_pages,
            page_size=page_size,
        )

    async def list_pull_request_reviews(self, number: int, *, max_pages: int = 1) -> GitHubListResult:
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/pulls/{number}/reviews",
            params={},
            max_pages=max_pages,
        )

    async def list_pull_request_comments(self, number: int, *, max_pages: int = 1) -> GitHubListResult:
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/issues/{number}/comments",
            params={"sort": "updated", "direction": "desc"},
            max_pages=max_pages,
        )

    async def list_pull_request_review_comments(self, number: int, *, max_pages: int = 1) -> GitHubListResult:
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/pulls/{number}/comments",
            params={"sort": "updated", "direction": "desc"},
            max_pages=max_pages,
        )

    async def list_workflow_runs(self, head_sha: str, *, max_pages: int = 1) -> GitHubListResult:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", head_sha):
            raise ValueError("A full or abbreviated commit SHA is required.")
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/actions/runs",
            params={"head_sha": head_sha},
            item_key="workflow_runs",
            max_pages=max_pages,
            page_size=20,
        )

    async def list_workflow_jobs(self, run_id: int, *, max_pages: int = 1) -> GitHubListResult:
        if run_id <= 0:
            raise ValueError("Workflow run id must be positive.")
        repo = self._require_configured()
        return await self._list_paginated(
            f"/repos/{repo}/actions/runs/{run_id}/jobs",
            params={},
            item_key="jobs",
            max_pages=max_pages,
        )

    async def get_commit_metadata(self, sha: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise ValueError("A full or abbreviated commit SHA is required.")
        repo = self._require_configured()
        return await self._read_request(f"/repos/{repo}/commits/{quote(sha, safe='')}")

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

    async def list_check_runs_result(self, ref: str, *, max_pages: int = 2) -> GitHubListResult:
        repo = self._require_configured()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", ref):
            raise ValueError("A full or abbreviated commit SHA is required.")
        return await self._list_paginated(
            f"/repos/{repo}/commits/{quote(ref, safe='')}/check-runs",
            params={},
            item_key="check_runs",
            max_pages=max_pages,
        )

    async def list_check_runs(self, ref: str) -> list[dict]:
        return (await self.list_check_runs_result(ref)).items

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
