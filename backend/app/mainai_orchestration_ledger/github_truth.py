"""GitHub as the software-truth source for orchestration.

Conversation text, agent claims, and prior chat SHAs are never consulted here.
A missing branch is `exists_remotely=False`, not an exception and not a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_orchestration_ledger.types import (
    GitHubCheckRun,
    GitHubDeploymentTruth,
    GitHubPullTruth,
    GitHubTruthSnapshot,
)


class SoftwareTruthSource(Protocol):
    async def inspect_branch(
        self,
        branch: str,
        *,
        local_sha: str | None = None,
        expected_sha: str | None = None,
    ) -> GitHubTruthSnapshot:
        """Live GitHub observation for one branch. Never inferred from chat."""


class FakeSoftwareTruthSource:
    """Deterministic GitHub stand-in for tests. Still a GitHub-shaped truth object:
    callers cannot sneak an agent claim in as `source`."""

    def __init__(
        self,
        *,
        branches: dict[str, str] | None = None,
        trees: dict[str, str] | None = None,
        check_runs: dict[str, list[GitHubCheckRun]] | None = None,
        pull_requests: dict[str, list[GitHubPullTruth]] | None = None,
        default_branch: str = "claude/det-kommer-mer-879lcm",
        default_branch_sha: str | None = None,
        deployments: dict[str, list[GitHubDeploymentTruth]] | None = None,
    ) -> None:
        self.branches = dict(branches or {})
        self.trees = dict(trees or {})
        self.check_runs = dict(check_runs or {})
        self.pull_requests = dict(pull_requests or {})
        self.default_branch = default_branch
        self.default_branch_sha = default_branch_sha
        self.deployments = dict(deployments or {})

    async def inspect_branch(
        self,
        branch: str,
        *,
        local_sha: str | None = None,
        expected_sha: str | None = None,
    ) -> GitHubTruthSnapshot:
        exists = branch in self.branches
        commit_sha = self.branches.get(branch)
        tree_sha = self.trees.get(commit_sha) if commit_sha else None
        sha_for_ci = expected_sha or commit_sha
        checks = tuple(self.check_runs.get(sha_for_ci, ())) if sha_for_ci else ()
        prs = tuple(self.pull_requests.get(branch, ()))
        deploys = tuple(self.deployments.get(sha_for_ci, ())) if sha_for_ci else ()
        match = None
        if exists and local_sha is not None:
            match = local_sha == commit_sha
        return GitHubTruthSnapshot(
            branch=branch,
            exists_remotely=exists,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            local_sha=local_sha,
            local_matches_remote=match,
            check_runs=checks,
            pull_requests=prs,
            default_branch=self.default_branch,
            default_branch_sha=self.default_branch_sha or self.branches.get(self.default_branch),
            deployments=deploys,
            captured_at=datetime.now(timezone.utc),
            source="github",
        )


class GitHubSoftwareTruthSource:
    """Read-only GitHub adapter. Write/merge/force-push/delete remain absent."""

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.client = client or GitHubClient()

    async def inspect_branch(
        self,
        branch: str,
        *,
        local_sha: str | None = None,
        expected_sha: str | None = None,
    ) -> GitHubTruthSnapshot:
        commit_sha = await self.client.get_ref_or_none(branch)
        exists = commit_sha is not None
        tree_sha = None
        if commit_sha:
            commit = await self.client.get_commit(commit_sha)
            tree_sha = (commit.get("tree") or {}).get("sha")
        sha_for_ci = expected_sha or commit_sha
        checks: tuple[GitHubCheckRun, ...] = ()
        deploys: tuple[GitHubDeploymentTruth, ...] = ()
        if sha_for_ci:
            raw_checks = await self.client.list_check_runs(sha_for_ci)
            checks = tuple(
                GitHubCheckRun(
                    name=item.get("name") or "",
                    status=item.get("status") or "",
                    conclusion=item.get("conclusion"),
                    head_sha=item.get("head_sha") or sha_for_ci,
                )
                for item in raw_checks
            )
            raw_deploys = await self.client.list_deployments(sha=sha_for_ci)
            deploys = tuple(
                GitHubDeploymentTruth(
                    id=int(item.get("id") or 0),
                    sha=item.get("sha") or sha_for_ci,
                    environment=item.get("environment") or "",
                    state=(item.get("statuses_url") and None) or item.get("state"),
                )
                for item in raw_deploys
            )
        repo = await self.client.get_repository()
        default_branch = repo.get("default_branch")
        default_sha = await self.client.get_ref_or_none(default_branch) if default_branch else None
        owner = (self.client.settings.github_repo or "").split("/", 1)[0]
        head = f"{owner}:{branch}" if owner else branch
        raw_prs: list[dict] = []
        if default_branch:
            try:
                raw_prs = await self.client.list_pull_requests_for_head(
                    head=head, base=default_branch, state="all"
                )
            except GitHubClientError:
                raw_prs = []
        prs = tuple(
            GitHubPullTruth(
                number=int(item.get("number") or 0),
                state=item.get("state") or "",
                merged=bool(item.get("merged_at") or item.get("merged")),
                head_sha=((item.get("head") or {}).get("sha")) or "",
                head_ref=((item.get("head") or {}).get("ref")) or branch,
                base_ref=((item.get("base") or {}).get("ref")) or "",
            )
            for item in raw_prs
        )
        match = None
        if exists and local_sha is not None:
            match = local_sha == commit_sha
        return GitHubTruthSnapshot(
            branch=branch,
            exists_remotely=exists,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            local_sha=local_sha,
            local_matches_remote=match,
            check_runs=checks,
            pull_requests=prs,
            default_branch=default_branch,
            default_branch_sha=default_sha,
            deployments=deploys,
            captured_at=datetime.now(timezone.utc),
            source="github",
        )


def answers_without_founder_relay(snapshot: GitHubTruthSnapshot) -> dict[str, object]:
    """The questions MainAI must answer from GitHub instead of asking the founder."""

    return {
        "branch_exists_remotely": snapshot.exists_remotely,
        "exact_branch_sha": snapshot.commit_sha,
        "exact_tree_hash": snapshot.tree_sha,
        "local_remote_match": snapshot.local_matches_remote,
        "ci_all_completed_success": snapshot.ci_all_completed_success,
        "ci_any_failure": snapshot.ci_any_failure,
        "ci_any_pending": snapshot.ci_any_pending,
        "open_pr": snapshot.any_open_pr,
        "merged": snapshot.any_merged_pr,
        "default_branch": snapshot.default_branch,
        "default_branch_sha": snapshot.default_branch_sha,
        "deployment_count": len(snapshot.deployments),
        "source": snapshot.source,
    }
