"""Concrete GET-only GitHub API transport with response provenance and error typing."""

from __future__ import annotations

from typing import Any

import httpx

from app.claim_action_integrity.adapters.base import (
    AdapterAuthenticationError,
    AdapterRateLimited,
    AdapterUnavailable,
    MalformedProviderResponse,
)


class GitHubAPIReader:
    """Read-only GitHub transport. It deliberately exposes no POST/PATCH/PUT/DELETE method."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ):
        if not token.strip():
            raise ValueError("GitHub token is required")
        self._client = client or httpx.Client(
            base_url=api_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "lifeai-authoritative-evidence-adapter",
            },
        )

    def get(self, resource: str) -> tuple[dict[str, Any], str]:
        response = self._get(resource)
        if response.status_code == 404:
            return {"exists": False}, self._response_identity(response)
        payload = self._object_payload(response)
        return payload, self._response_identity(response)

    def get_workflow_run(self, owner: str, repo: str, run_id: int) -> tuple[dict[str, Any], str]:
        response = self._get(f"repos/{owner}/{repo}/actions/runs/{run_id}")
        return self._object_payload(response), self._response_identity(response)

    def get_workflow_jobs(self, owner: str, repo: str, run_id: int) -> tuple[list[dict[str, Any]], str]:
        response = self._get(f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        payload = self._object_payload(response)
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise MalformedProviderResponse("GitHub jobs response has no jobs array")
        return jobs, self._response_identity(response)

    def _get(self, resource: str) -> httpx.Response:
        try:
            response = self._client.get(f"/{resource.lstrip('/')}")
        except httpx.HTTPError as exc:
            raise AdapterUnavailable("GitHub request failed") from exc
        if response.status_code == 401:
            raise AdapterAuthenticationError("GitHub authentication failed")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise AdapterRateLimited("GitHub rate limit exhausted")
        if response.status_code in {429}:
            raise AdapterRateLimited("GitHub rate limited the request")
        if response.status_code >= 500:
            raise AdapterUnavailable(f"GitHub unavailable ({response.status_code})")
        if response.status_code >= 400 and response.status_code != 404:
            raise MalformedProviderResponse(f"unexpected GitHub response status {response.status_code}")
        return response

    @staticmethod
    def _object_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedProviderResponse("GitHub response is not JSON") from exc
        if not isinstance(payload, dict):
            raise MalformedProviderResponse("GitHub response is not an object")
        return payload

    @staticmethod
    def _response_identity(response: httpx.Response) -> str:
        request_id = response.headers.get("x-github-request-id")
        etag = response.headers.get("etag")
        if not request_id:
            raise MalformedProviderResponse("GitHub response identity header is missing")
        return f"github-request:{request_id}:etag:{etag or 'none'}"

    def close(self) -> None:
        self._client.close()
