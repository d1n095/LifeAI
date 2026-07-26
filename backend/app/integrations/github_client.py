"""Minimal GitHub REST client for MainAI Core's agent orchestration (see
app/agent_orchestration.py). Deliberately narrow, mirroring the same swappable-adapter shape
as app/providers/*.py (a plain httpx-based client, no SDK dependency):

  - read: get_ref, get_pull_request, list_check_runs
  - write (only ever called when settings.github_write_enabled is True — see
    prepare_github_pr()): create_branch, create_or_update_file, create_pull_request,
    update_pull_request

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

import httpx

from app.config import get_settings

API_BASE = "https://api.github.com"


class GitHubClientError(RuntimeError):
    pass


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
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{API_BASE}{path}", headers=self._headers(), json=json)
            if resp.status_code >= 400:
                raise GitHubClientError(f"GitHub {method} {path} misslyckades ({resp.status_code}): {resp.text[:500]}")
            return resp.json() if resp.content else {}

    # --- Read ---------------------------------------------------------------------------

    async def get_ref(self, branch: str) -> str:
        """Returns the commit SHA a branch currently points at."""
        repo = self._require_configured()
        data = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def get_pull_request(self, number: int) -> dict:
        repo = self._require_configured()
        return await self._request("GET", f"/repos/{repo}/pulls/{number}")

    async def list_check_runs(self, ref: str) -> list[dict]:
        repo = self._require_configured()
        data = await self._request("GET", f"/repos/{repo}/commits/{ref}/check-runs")
        return data.get("check_runs", [])

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


def get_github_client() -> GitHubClient:
    return GitHubClient()
