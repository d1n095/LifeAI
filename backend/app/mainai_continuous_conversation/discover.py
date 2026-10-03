"""Read software truth from GitHub instead of asking the founder to relay it.

Read-only. Never merges, force-pushes, deploys, or activates Recall.
"""

from __future__ import annotations

import logging
import re

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.mainai_continuous_conversation.types import SoftwareTruth

logger = logging.getLogger(__name__)

FROZEN_FOUNDER_ALPHA_BRANCH = "codex/founder-alpha-final-composed-candidate"
_BRANCH_IN_TEXT = re.compile(r"\b([\w./-]+/[\w./-]+)\b")


def branch_from_founder_text(text: str) -> str:
    lowered = text.lower()
    if "founder alpha" in lowered or "frozen" in lowered or "2fbe20a" in lowered:
        return FROZEN_FOUNDER_ALPHA_BRANCH
    match = _BRANCH_IN_TEXT.search(text)
    if match and "sha" not in match.group(1).lower():
        return match.group(1)
    return FROZEN_FOUNDER_ALPHA_BRANCH


async def discover_software_truth(text: str, *, client: GitHubClient | None = None) -> SoftwareTruth:
    branch = branch_from_founder_text(text)
    github = client or GitHubClient()
    if not github.is_configured():
        return SoftwareTruth(
            branch=branch,
            source="unavailable",
            detail="GitHub is not configured — MainAI still must not ask the founder to relay the SHA",
        )
    try:
        sha = await github.get_ref(branch)
    except (GitHubClientError, Exception) as exc:
        logger.info("software-truth discovery failed for %s: %s", branch, exc)
        return SoftwareTruth(branch=branch, source="unavailable", detail=str(exc)[:240])
    ci_summary = None
    try:
        runs = await github.list_check_runs(sha)
        if runs:
            counts: dict[str, int] = {}
            for run in runs:
                conclusion = str(run.get("conclusion") or run.get("status") or "unknown")
                counts[conclusion] = counts.get(conclusion, 0) + 1
            ci_summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    except (GitHubClientError, Exception) as exc:
        logger.info("CI discovery failed for %s: %s", sha, exc)
    return SoftwareTruth(branch=branch, sha=sha, ci_summary=ci_summary, source="github", detail="read from GitHub ref")
