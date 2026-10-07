"""Resolve software truth by binding the requested subject, then reading that source.

Never answers with whichever branch happens to be current. Never uses checkout SHA.
Never runs blocking git. If the subject cannot be bound or the source is unavailable:
UNKNOWN. Never fabricate. Never ask the founder to relay.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.integrations.github_client import GitHubClient
from app.mainai_continuous_conversation.entities import (
    bind_subject_from_db,
    classify_requested_entity,
    record_observation,
)
from app.mainai_continuous_conversation.provider import (
    AuthoritativeStateProvider,
    GitHubAuthoritativeStateProvider,
    ObservedRepositoryState,
)
from app.mainai_continuous_conversation.types import ArtifactRole, SoftwareTruth

logger = logging.getLogger(__name__)


async def _ci_summary(github: GitHubClient, sha: str) -> str | None:
    try:
        runs = await github.list_check_runs(sha)
    except Exception as exc:
        logger.info("CI discovery failed for %s: %s", sha, exc)
        return None
    if not runs:
        return None
    counts: dict[str, int] = {}
    for run in runs:
        conclusion = str(run.get("conclusion") or run.get("status") or "unknown")
        counts[conclusion] = counts.get(conclusion, 0) + 1
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def _unknown(*, entity_key: str = "unspecified", branch: str = "unspecified", detail: str) -> SoftwareTruth:
    return SoftwareTruth(
        branch=branch,
        entity_key=entity_key,
        source="unavailable",
        detail=detail,
    )


async def discover_software_truth(
    text: str,
    *,
    db: Session | None = None,
    client: GitHubClient | None = None,
    provider: AuthoritativeStateProvider | None = None,
) -> SoftwareTruth:
    if db is None:
        return _unknown(detail="No governed registry session. UNKNOWN — will not invent state.")

    github = client or GitHubClient()
    adapter = provider or GitHubAuthoritativeStateProvider(github)
    key = classify_requested_entity(text)
    observed: ObservedRepositoryState | None = None

    from app.mainai_continuous_conversation.entities import load_entity_record

    record = load_entity_record(db, key) if key else None
    needs_live_tip = record is not None and record.artifact_role == ArtifactRole.CURRENT_BRANCH_TIP.value
    named_needs_tip = key is None and any(prefix in text for prefix in ("cursor/", "codex/", "claude/"))
    if needs_live_tip or named_needs_tip:
        repository = record.repository if record is not None else "d1n095/LifeAI"
        branch = record.branch if record is not None else ""
        if not branch:
            from app.mainai_continuous_conversation.entities import NAMED_BRANCH

            match = NAMED_BRANCH.search(text)
            branch = match.group(1) if match else ""
        if branch:
            observed = await adapter.observe_branch_tip(repository, branch)
            if observed.sha:
                record_observation(db, observed)

    subject = bind_subject_from_db(db, text, observed=observed)
    if subject is None:
        return _unknown(detail="No requested entity could be bound. UNKNOWN — will not default to the current branch.")

    if subject.artifact_role is not ArtifactRole.CURRENT_BRANCH_TIP and subject.sha:
        ci_summary = None
        if github.is_configured():
            ci_summary = await _ci_summary(github, subject.sha)
        return subject.as_software_truth(ci_summary=ci_summary)

    if subject.sha:
        ci_summary = None
        if github.is_configured():
            ci_summary = await _ci_summary(github, subject.sha)
        return subject.as_software_truth(ci_summary=ci_summary)

    return subject.as_software_truth()


def requested_entity_key(text: str) -> str | None:
    return classify_requested_entity(text)


def branch_from_founder_text(text: str, *, db: Session | None = None) -> str:
    if db is None:
        return "unspecified"
    subject = bind_subject_from_db(db, text)
    if subject is None:
        return "unspecified"
    return subject.branch
