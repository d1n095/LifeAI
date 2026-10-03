"""Outbound founder-message policy.

Routine coordination stays internal. Never ask the founder to paste a SHA, branch,
test result, wait-graph, or agent message that MainAI can discover itself.
"""

from __future__ import annotations

import re

from app.mainai_cognitive_ops.founder_anti_repetition import assess_communication_necessity
from app.mainai_continuous_conversation.types import OutboundDecision, OutboundDisposition, RelayCategory, SoftwareTruth

_SAFE_REWRITE = (
    "MainAI handles machine coordination internally. GitHub is the software-truth source "
    "for SHAs, branches, CI, and PRs. I will not ask you to relay agent messages or git facts."
)

_PATTERNS: tuple[tuple[RelayCategory, re.Pattern[str]], ...] = (
    (RelayCategory.SHA, re.compile(r"\b(paste|send|tell me|what is|give me).{0,40}\b(sha|commit hash)\b", re.I)),
    (RelayCategory.SHA, re.compile(r"\b(exact sha|which sha|the sha is)\b", re.I)),
    (RelayCategory.BRANCH_NAME, re.compile(r"\b(paste|tell me|which).{0,40}\bbranch\b", re.I)),
    (RelayCategory.TEST_RESULT, re.compile(r"\b(paste|forward|tell me).{0,40}\b(test results?|pytest)\b", re.I)),
    (RelayCategory.CI_STATE, re.compile(r"\b(is ci green|ci status|github actions)\b", re.I)),
    (RelayCategory.AGENT_MESSAGE, re.compile(r"\b(please (tell|ask|forward|relay) (this to )?(claude|cursor|codex))\b", re.I)),
    (RelayCategory.NEXT_AGENT, re.compile(r"\b(who should work next|which agent next)\b", re.I)),
    (RelayCategory.WAIT_GRAPH, re.compile(r"\b(who is waiting for whom)\b", re.I)),
)


def detect_relay_categories(text: str) -> tuple[RelayCategory, ...]:
    found: list[RelayCategory] = []
    for category, pattern in _PATTERNS:
        if pattern.search(text) and category not in found:
            found.append(category)
    return tuple(found)


def filter_outbound(text: str, *, discovered: SoftwareTruth | None = None) -> OutboundDecision:
    blocked = detect_relay_categories(text)
    if blocked:
        content = (discovered.founder_answer if discovered is not None else None) or _SAFE_REWRITE
        return OutboundDecision(
            disposition=OutboundDisposition.REWRITE,
            original=text,
            content=content,
            blocked_categories=blocked,
            reason="outbound text asked the founder to relay machine-discoverable facts",
        )
    return OutboundDecision(
        disposition=OutboundDisposition.SEND,
        original=text,
        content=text,
        reason="no founder-relay request detected",
    )


def should_notify_founder(*, previous: dict | None, candidate_status: str, decision_now_required: bool) -> bool:
    return assess_communication_necessity(
        previous=previous,
        candidate_status=candidate_status,
        decision_now_required=decision_now_required,
    ).should_notify
