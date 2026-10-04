"""Inbound founder-utterance classification.

Agent coordination, SHA/branch/CI questions, and wait-graphs are work for MainAI,
not a reason to interrupt the founder. Founder-only authority utterances must gate
behavior, not merely produce a label. English and Swedish.
"""

from __future__ import annotations

import re

from app.mainai_continuous_conversation.entities import classify_requested_entity
from app.mainai_continuous_conversation.types import InboundClassification, InboundKind, InterruptKind, RelayCategory

_SHA_ASK = re.compile(
    r"("
    r"\b(sha|commit hash|tree hash|tree id|git rev-parse|commit id|commit-id)\b"
    r"|sha:n|klistra in sha|klistra in sha:n"
    r"|could you share the commit"
    r"|vad [äa]r .*sha"
    r")",
    re.IGNORECASE,
)
_BRANCH_ASK = re.compile(
    r"\b(branch name|which branch|working branch|remote branch|vilken gren|branchnamn)\b",
    re.IGNORECASE,
)
_TEST_ASK = re.compile(
    r"("
    r"\b(test results?|pytest|ci (status|state|green|red)|github actions?)\b"
    r"|[äa]r ci gr[öo]nt|ci gr[öo]nt"
    r")",
    re.IGNORECASE,
)
_AGENT_RELAY = re.compile(
    r"("
    r"\b(tell (claude|cursor|codex)|ask (claude|cursor|codex)|relay|paste (this|the sha|it) (to|into))\b"
    r"|vad sa (cursor|claude|codex)"
    r"|what did (cursor|claude|codex) (report|say|write)"
    r")",
    re.IGNORECASE,
)
_WAIT_GRAPH = re.compile(
    r"\b(who is waiting|who should work next|next agent|blocked by whom|vem v[äa]ntar)\b",
    re.IGNORECASE,
)
_SPEND = re.compile(
    r"("
    r"\b(approve|authorization|budget|spend|invoice|\$\d+)\b"
    r"|godk[äa]nn k[öo]pet|godk[äa]nn k[öo]p"
    r")",
    re.IGNORECASE,
)
_SECURITY = re.compile(
    r"("
    r"\b(security policy|rls|secret|credential|force[- ]push|deploy to prod)\b"
    r"|s[äa]kerhetspolicyn|[äa]ndra s[äa]kerhet"
    r")",
    re.IGNORECASE,
)
_DESTRUCTIVE = re.compile(
    r"("
    r"\b(delete branch|drop database|wipe|destroy|rm -rf)\b"
    r"|radera grenen|radera branchen|ta bort grenen"
    r")",
    re.IGNORECASE,
)
_PRODUCT = re.compile(
    r"("
    r"\b(product decision|should we ship|should we launch|should we release|rename the product|change the ux contract)\b"
    r"|ska vi lansera|lansera nu"
    r")",
    re.IGNORECASE,
)
_SWEDISH = re.compile(
    r"[åäöÅÄÖ]|(radera|grenen|lansera|s[äa]kerhetspolicyn|godk[äa]nn|k[öo]pet|klistra|gr[öo]nt|vad sa)",
    re.IGNORECASE,
)


def _language(text: str) -> str:
    return "sv" if _SWEDISH.search(text) else "en"


def classify_inbound(text: str) -> InboundClassification:
    lowered = text.strip()
    if not lowered:
        return InboundClassification(InboundKind.ORDINARY_CHAT, reason="empty")

    language = _language(lowered)
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
        reason = {
            InterruptKind.DESTRUCTIVE_ACTION: (
                "Grundarauktoritet krävs: destruktiv åtgärd stoppas. Jag utför den inte."
                if language == "sv"
                else "Founder authority required: destructive action is gated and will not be executed."
            ),
            InterruptKind.SECURITY_POLICY: (
                "Grundarauktoritet krävs: säkerhetspolicy ändras inte av den här ytan."
                if language == "sv"
                else "Founder authority required: security policy will not be changed on this surface."
            ),
            InterruptKind.MONEY_BUDGET: (
                "Grundarauktoritet krävs: köp/budget godkänns inte av den här ytan."
                if language == "sv"
                else "Founder authority required: spend/purchase will not be approved on this surface."
            ),
            InterruptKind.MATERIAL_PRODUCT_DECISION: (
                "Grundarauktoritet krävs: lansering beslutas inte av den här ytan."
                if language == "sv"
                else "Founder authority required: launch/ship will not be decided on this surface."
            ),
        }[interrupt]
        return InboundClassification(
            InboundKind.AUTHORITY_REQUEST,
            interrupt=interrupt,
            reason=reason,
            language=language,
        )

    categories: list[RelayCategory] = []
    if classify_requested_entity(lowered) is not None:
        categories.append(RelayCategory.SHA)
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
            language=language,
        )

    if re.search(
        r"\b(status|progress|what is (claude|cursor|codex) doing|vad g[öo]r (claude|cursor|codex))\b",
        lowered,
        re.IGNORECASE,
    ):
        return InboundClassification(
            InboundKind.STATUS_QUESTION,
            reason="status is an internal occupancy question",
            language=language,
        )

    if re.search(r"\b(assign|examine|validate|build|implement|tilldela|granska)\b", lowered, re.IGNORECASE):
        return InboundClassification(
            InboundKind.WORK_COMMAND,
            reason="work command for MainAI to coordinate internally",
            language=language,
        )

    return InboundClassification(InboundKind.ORDINARY_CHAT, reason="ordinary founder chat", language=language)
