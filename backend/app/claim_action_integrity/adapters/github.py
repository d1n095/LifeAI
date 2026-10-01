"""Read-only normalization of GitHub observations; no mutation methods exist here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.base import MalformedProviderResponse, ingest_observation, provider_failure
from app.claim_action_integrity.adapters.types import EvidenceContext, FactMutability, ProviderObservation


class GitHubReader(Protocol):
    def get(self, resource: str) -> tuple[dict[str, Any], str]: ...


class GitHubEvidenceAdapter:
    def __init__(self, reader: GitHubReader, *, mutable_max_age_seconds: int = 300):
        self.reader = reader
        self.mutable_max_age_seconds = mutable_max_age_seconds

    def _observe(self, db: Session, context: EvidenceContext, resource: str, normalize):
        try:
            response, response_id = self.reader.get(resource)
            observation = normalize(response, response_id)
            return ingest_observation(db, context, observation)
        except Exception as exc:
            return provider_failure(exc)

    def observe_branch(self, db: Session, context: EvidenceContext, *, owner: str, repo: str, branch: str, local_sha: str | None = None):
        def normalize(data: dict[str, Any], response_id: str) -> ProviderObservation:
            if data.get("exists") is False:
                return ProviderObservation(
                    "github", f"github:{owner}/{repo}:refs/heads/{branch}", response_id,
                    FactMutability.mutable_snapshot,
                    _bindings(context, repository=f"{owner}/{repo}", branch=branch),
                    {"remote_exists": False, "branch": branch}, _now(), self.mutable_max_age_seconds,
                )
            sha = data.get("object", {}).get("sha")
            if data.get("ref") != f"refs/heads/{branch}" or not isinstance(sha, str):
                raise MalformedProviderResponse("branch response is incomplete")
            bindings = _bindings(context, repository=f"{owner}/{repo}", branch=branch, sha=sha)
            facts = {"pushed": True, "branch": branch, "remote_sha": sha, "local_sha": local_sha, "remote_exists": True}
            return ProviderObservation("github", f"github:{owner}/{repo}:refs/heads/{branch}", response_id, FactMutability.mutable_snapshot, bindings, facts, _now(), self.mutable_max_age_seconds)
        return self._observe(db, context, f"repos/{owner}/{repo}/git/ref/heads/{branch}", normalize)

    def observe_commit(self, db: Session, context: EvidenceContext, *, owner: str, repo: str, sha: str):
        def normalize(data: dict[str, Any], response_id: str) -> ProviderObservation:
            tree = data.get("commit", {}).get("tree", {}).get("sha")
            if data.get("sha") != sha or not isinstance(tree, str):
                raise MalformedProviderResponse("commit response is not bound to requested SHA")
            parents = [item.get("sha") for item in data.get("parents", []) if isinstance(item.get("sha"), str)]
            return ProviderObservation("github", f"github:{owner}/{repo}:commit/{sha}", response_id, FactMutability.immutable_fact, _bindings(context, repository=f"{owner}/{repo}", sha=sha, tree_hash=tree), {"commit_exists": True, "tree_hash": tree, "parents": parents}, _now())
        return self._observe(db, context, f"repos/{owner}/{repo}/commits/{sha}", normalize)

    def observe_pull_request(self, db: Session, context: EvidenceContext, *, owner: str, repo: str, number: int, expected_head_ref: str, expected_head_sha: str):
        def normalize(data: dict[str, Any], response_id: str) -> ProviderObservation:
            head = data.get("head", {})
            if head.get("ref") != expected_head_ref or head.get("sha") != expected_head_sha:
                raise MalformedProviderResponse("pull request head binding mismatch")
            facts = {"pr_number": number, "state": data.get("state"), "merged": data.get("merged") is True, "merge_sha": data.get("merge_commit_sha")}
            return ProviderObservation("github", f"github:{owner}/{repo}:pull/{number}", response_id, FactMutability.mutable_snapshot, _bindings(context, repository=f"{owner}/{repo}", branch=expected_head_ref, sha=expected_head_sha, pull_request=str(number)), facts, _now(), self.mutable_max_age_seconds)
        return self._observe(db, context, f"repos/{owner}/{repo}/pulls/{number}", normalize)

    def observe_repository(self, db: Session, context: EvidenceContext, *, owner: str, repo: str):
        def normalize(data: dict[str, Any], response_id: str) -> ProviderObservation:
            if data.get("full_name") != f"{owner}/{repo}" or not data.get("default_branch"):
                raise MalformedProviderResponse("repository identity mismatch")
            return ProviderObservation("github", f"github:{owner}/{repo}", response_id, FactMutability.mutable_snapshot, _bindings(context, repository=f"{owner}/{repo}"), {"default_branch": data["default_branch"]}, _now(), self.mutable_max_age_seconds)
        return self._observe(db, context, f"repos/{owner}/{repo}", normalize)

    def observe_commit_ancestry(self, db: Session, context: EvidenceContext, *, owner: str, repo: str, base_sha: str, head_sha: str):
        def normalize(data: dict[str, Any], response_id: str) -> ProviderObservation:
            status = data.get("status")
            if status not in {"ahead", "behind", "diverged", "identical"}:
                raise MalformedProviderResponse("comparison status is invalid")
            if (
                data.get("base_commit", {}).get("sha") != base_sha
                or data.get("head_commit", {}).get("sha") != head_sha
                or data.get("merge_base_commit", {}).get("sha") is None
            ):
                raise MalformedProviderResponse("comparison commit binding mismatch")
            facts = {
                "comparison_status": status,
                "base_is_ancestor_of_head": status in {"ahead", "identical"},
                "ahead_by": data.get("ahead_by"),
                "behind_by": data.get("behind_by"),
                "merge_base_sha": data["merge_base_commit"]["sha"],
            }
            bindings = _bindings(context, repository=f"{owner}/{repo}", base_sha=base_sha, sha=head_sha)
            return ProviderObservation("github", f"github:{owner}/{repo}:compare/{base_sha}...{head_sha}", response_id, FactMutability.immutable_fact, bindings, facts, _now())
        return self._observe(db, context, f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}", normalize)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bindings(context: EvidenceContext, **values: str) -> dict[str, str]:
    return {"owner_id": str(context.owner_id), "execution_id": context.execution_id, "subject": context.subject_key, "task_id": context.task_id or "", **values}
