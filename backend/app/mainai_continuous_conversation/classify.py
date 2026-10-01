"""Inbound founder-utterance classification.

Agent coordination, SHA/branch/CI questions, and wait-graphs are work for MainAI,
not a reason to interrupt the founder.
"""

from __future__ import annotations

import re

from app.mainai_continuous_conversation.types import InboundClassification, InboundKind, InterruptKind, RelayCategory

_SHA_ASK = re.compile(
    r"\b(sha|commit hash|tree hash|tree id|git rev-parse)\b",
    re.IGNORECASE,
)
_BRANCH_ASK = re.compile(
    r"\b(branch name|which branch|working branch|remote branch)\b",
    re.IGNORECASE,
)
_TEST_ASK = re.compile(
    r"\b(test results?|pytest|ci (status|state|green|red)|github actions?)\b",
    re.IGNORECASE,
)
_AGENT_RELAY = re.compile(
    r"\b(tell (claude|cursor|codex)|ask (claude|cursor|codex)|relay|paste (this|the sha|it) (to|into))\b",
    re.IGNORECASE,
)
_WAIT_GRAPH = re.compile(
    r"\b(who is waiting|who should work next|next agent|blocked by whom)\b",
    re.IGNORECASE,
)
_SPEND = re.compile(r"\b(approve|authorization|budget|spend|invoice|\$\d+)\b", re.IGNORECASE)
_SECURITY = re.compile(r"\b(security policy|rls|secret|credential|force[- ]push|deploy to prod)\b", re.IGNORECASE)
_DESTRUCTIVE = re.compile(r"\b(delete branch|drop database|wipe|destroy|rm -rf)\b", re.IGNORECASE)
_PRODUCT = re.compile(r"\b(product decision|should we ship|rename the product|change the ux contract)\b", re.IGNORECASE)


def classify_inbound(text: str) -> InboundClassification:
    lowered = text.strip()
    if not lowered:
        return InboundClassification(InboundKind.ORDINARY_CHAT, reason="empty")

    interrupt = InterruptKind.NONE
    if _DESTRUCTIVE.search(lowered):
        interrupt = InterruptKind.DESTRUCTIVE_ACTION
    elif _SECURITY.search(lowered) and _SPEND.search(lowered) is None:
        interrupt = InterruptKind.SECURITY_POLICY
    elif _SPEND.search(lowered):
        interrupt = InterruptKind.MONEY_BUDGET
    elif _PRODUCT.search(lowered):
        interrupt = InterruptKind.MATERIAL_PRODUCT_DECISION

    if interrupt is not InterruptKind.NONE:
        return InboundClassification(
            InboundKind.AUTHORITY_REQUEST,
            interrupt=interrupt,
            reason=f"founder authority required: {interrupt.value}",
        )

    categories: list[RelayCategory] = []
    if _SHA_ASK.search(lowered):
        categories.append(RelayCategory.SHA)
    if _BRANCH_ASK.search(lowered):
        categories.append(RelayCategory.BRANCH_NAME)
    if _TEST_ASK.search(lowered):
        categories.append(RelayCategory.TEST_RESULT)
        categories.append(RelayCategory.CI_STATE)
    if _AGENT_RELAY.search(lowered):
        categories.append(RelayCategory.AGENT_MESSAGE)
        categories.append(RelayCategory.NEXT_AGENT)
    if _WAIT_GRAPH.search(lowered):
        categories.append(RelayCategory.WAIT_GRAPH)
        categories.append(RelayCategory.NEXT_AGENT)

    if categories:
        return InboundClassification(
            InboundKind.RELAY_REQUEST,
            relay_categories=tuple(dict.fromkeys(categories)),
            reason="GitHub/agent facts are discovered internally — do not ask the founder to relay them",
        )

    if re.search(r"\b(status|progress|what is (claude|cursor|codex) doing)\b", lowered, re.IGNORECASE):
        return InboundClassification(InboundKind.STATUS_QUESTION, reason="status is an internal occupancy question")

    if re.search(r"\b(assign|examine|validate|build|implement)\b", lowered, re.IGNORECASE):
        return InboundClassification(InboundKind.WORK_COMMAND, reason="work command for MainAI to coordinate internally")

    return InboundClassification(InboundKind.ORDINARY_CHAT, reason="ordinary founder chat")
