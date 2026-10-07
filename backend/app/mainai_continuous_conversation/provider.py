"""Asynchronous authoritative repository-state adapter.

Production must not require a local git checkout. Blocking `git ls-remote` is forbidden
on the request path. Frozen SHAs come from governed certification records; current tips
come from GitHub or a pre-fetched observation row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.integrations.github_client import GitHubClient, GitHubClientError


@dataclass(frozen=True)
class ObservedRepositoryState:
    repository: str
    branch: str
    sha: str | None
    source: str
    observed_at: datetime | None
    detail: str = ""


class AuthoritativeStateProvider:
    """Async adapter. Implementations must not spawn blocking git subprocesses."""

    async def observe_branch_tip(self, repository: str, branch: str) -> ObservedRepositoryState:
        raise NotImplementedError


class GitHubAuthoritativeStateProvider(AuthoritativeStateProvider):
    def __init__(self, client: GitHubClient | None = None) -> None:
        self.client = client or GitHubClient()

    async def observe_branch_tip(self, repository: str, branch: str) -> ObservedRepositoryState:
        if not self.client.is_configured():
            return ObservedRepositoryState(
                repository=repository,
                branch=branch,
                sha=None,
                source="unavailable",
                observed_at=None,
                detail="GitHub is not configured. UNKNOWN — checkout is not used.",
            )
        try:
            sha = await self.client.get_ref(branch)
        except (GitHubClientError, Exception) as exc:
            return ObservedRepositoryState(
                repository=repository,
                branch=branch,
                sha=None,
                source="unavailable",
                observed_at=None,
                detail=f"GitHub ref lookup failed ({exc}). UNKNOWN — checkout is not used.",
            )
        return ObservedRepositoryState(
            repository=repository,
            branch=branch,
            sha=sha.lower() if sha else None,
            source="github_ref",
            observed_at=datetime.now(timezone.utc),
            detail=f"Observed {repository}@{branch} from GitHub.",
        )


class PrefetchedStateProvider(AuthoritativeStateProvider):
    """Test/production adapter that serves already-observed authoritative rows. No git."""

    def __init__(self, observations: dict[tuple[str, str], ObservedRepositoryState] | None = None) -> None:
        self.observations = observations or {}

    async def observe_branch_tip(self, repository: str, branch: str) -> ObservedRepositoryState:
        found = self.observations.get((repository, branch))
        if found is not None:
            return found
        return ObservedRepositoryState(
            repository=repository,
            branch=branch,
            sha=None,
            source="unavailable",
            observed_at=None,
            detail="No pre-fetched observation. UNKNOWN — checkout is not used.",
        )
