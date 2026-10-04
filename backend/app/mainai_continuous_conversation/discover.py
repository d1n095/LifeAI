"""Resolve software truth by binding the requested subject, then reading that source.

Never answers with whichever branch happens to be current. Never uses checkout SHA
unless that is the requested entity. If the subject cannot be bound or the source is
unavailable: UNKNOWN. Never fabricate. Never ask the founder to relay.
"""

from __future__ import annotations

import logging

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_continuous_conversation.entities import (
    FROZEN_FOUNDER_ALPHA_BRANCH,
    bind_subject,
    classify_requested_entity,
    read_remote_branch_sha,
)
from app.mainai_continuous_conversation.types import ArtifactRole, SoftwareTruth

logger = logging.getLogger(__name__)

FROZEN_FOUNDER_ALPHA_BRANCH = FROZEN_FOUNDER_ALPHA_BRANCH


def branch_from_founder_text(text: str) -> str:
    subject = bind_subject(text)
    if subject is not None:
        return subject.branch
    return FROZEN_FOUNDER_ALPHA_BRANCH


async def _ci_summary(github: GitHubClient, sha: str) -> str | None:
    try:
        runs = await github.list_check_runs(sha)
    except (GitHubClientError, Exception) as exc:
        logger.info("CI discovery failed for %s: %s", sha, exc)
        return None
    if not runs:
        return None
    counts: dict[str, int] = {}
    for run in runs:
        conclusion = str(run.get("conclusion") or run.get("status") or "unknown")
        counts[conclusion] = counts.get(conclusion, 0) + 1
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))


async def discover_software_truth(text: str, *, client: GitHubClient | None = None) -> SoftwareTruth:
    subject = bind_subject(text)
    if subject is None:
        return SoftwareTruth(
            branch="unspecified",
            entity_key="unspecified",
            source="unavailable",
            detail="No requested entity could be bound. UNKNOWN — will not default to the current branch.",
        )
    if subject.artifact_role is not ArtifactRole.CURRENT_BRANCH_TIP and subject.sha:
        ci_summary = None
        github = client or GitHubClient()
        if github.is_configured():
            ci_summary = await _ci_summary(github, subject.sha)
        return subject.as_software_truth(ci_summary=ci_summary)

    github = client or GitHubClient()
    sha = subject.sha
    source = subject.authoritative_source
    if sha is None and github.is_configured():
        try:
            sha = await github.get_ref(subject.branch)
            source = "github_ref"
        except (GitHubClientError, Exception) as exc:
            logger.info("software-truth discovery failed for bound branch %s: %s", subject.branch, exc)
            sha = read_remote_branch_sha(subject.branch)
            source = "remote_branch_tip" if sha else "unavailable"
    elif sha is None:
        sha = read_remote_branch_sha(subject.branch)
        source = "remote_branch_tip" if sha else "unavailable"

    ci_summary = None
    if sha and github.is_configured():
        ci_summary = await _ci_summary(github, sha)
    return SoftwareTruth(
        branch=subject.branch,
        sha=sha,
        ci_summary=ci_summary,
        source=source if sha else "unavailable",
        detail=subject.detail if sha else "Authoritative tip unavailable. UNKNOWN — checkout is not used.",
        entity_key=subject.entity_key,
        repository=subject.repository,
        artifact_role=subject.artifact_role.value,
        state=subject.state,
    )


def requested_entity_key(text: str) -> str | None:
    return classify_requested_entity(text)
