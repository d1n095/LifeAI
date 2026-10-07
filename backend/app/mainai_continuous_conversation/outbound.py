"""Outbound founder-message policy.

Classify intent/context first. Never rewrite an entire educational answer merely because
it contains GitHub, CI, branch, commit, test, or SHA. Only rewrite when the reply asks
the founder to relay a machine-discoverable fact.
"""

from __future__ import annotations

import re

from app.mainai_cognitive_ops.founder_anti_repetition import assess_communication_necessity
from app.mainai_continuous_conversation.capability import capability_disclaimer, unknown_lookup_reply
from app.mainai_continuous_conversation.types import OutboundDecision, OutboundDisposition, RelayCategory, SoftwareTruth

_SAFE_REWRITE = capability_disclaimer()

_EDUCATIONAL = re.compile(
    r"("
    r"\b(you can configure|how to (configure|set up|run)|status page lists|"
    r"documentation|for example|e\.g\.|example:|typically|in general|"
    r"github actions to run pytest|ci status page)\b"
    r"|du kan konfigurera|till exempel"
    r")",
    re.IGNORECASE,
)

_ASK_FOUNDER_RELAY = re.compile(
    r"("
    r"\b(please |could you |can you )?(paste|send|forward|share|tell me|give me|drop)\b.{0,80}\b"
    r"(sha|commit( hash| id)?|branch|test results?|pytest|ci|github actions?|agent (report|message))\b"
    r"|\b(kan du (klistra in|skicka|ber[äa]tta)|klistra in).{0,80}\b(sha|sha:n|commit|testresultat|ci)\b"
    r"|\b(please (tell|ask|forward|relay) (this to )?(claude|cursor|codex))\b"
    r"|\b(vad sa (cursor|claude|codex)|what did (cursor|claude|codex) (report|say))\b"
    r"|\b(who should work next|which agent next|who is waiting for whom)\b"
    r"|\b[äa]r ci gr[öo]nt\b"
    r")",
    re.IGNORECASE,
)

_CATEGORY_HINTS: tuple[tuple[RelayCategory, re.Pattern[str]], ...] = (
    (RelayCategory.SHA, re.compile(r"\b(sha|commit( hash| id)?)\b", re.I)),
    (RelayCategory.BRANCH_NAME, re.compile(r"\bbranch\b", re.I)),
    (RelayCategory.TEST_RESULT, re.compile(r"\b(test results?|pytest)\b", re.I)),
    (RelayCategory.CI_STATE, re.compile(r"\b(ci|github actions?)\b", re.I)),
    (RelayCategory.AGENT_MESSAGE, re.compile(r"\b(claude|cursor|codex)\b", re.I)),
    (RelayCategory.WAIT_GRAPH, re.compile(r"\bwaiting for whom\b", re.I)),
    (RelayCategory.NEXT_AGENT, re.compile(r"\bwho should work next\b", re.I)),
)


def is_educational_reply(text: str) -> bool:
    return bool(_EDUCATIONAL.search(text)) and not _ASK_FOUNDER_RELAY.search(text)


def detect_relay_categories(text: str) -> tuple[RelayCategory, ...]:
    if is_educational_reply(text) or not _ASK_FOUNDER_RELAY.search(text):
        return ()
    found: list[RelayCategory] = []
    for category, pattern in _CATEGORY_HINTS:
        if pattern.search(text) and category not in found:
            found.append(category)
    return tuple(found) or (RelayCategory.SHA,)


def filter_outbound(text: str, *, discovered: SoftwareTruth | None = None) -> OutboundDecision:
    if is_educational_reply(text):
        return OutboundDecision(
            disposition=OutboundDisposition.SEND,
            original=text,
            content=text,
            reason="educational/context reply preserved — word presence is not relay intent",
        )
    blocked = detect_relay_categories(text)
    if blocked:
        if discovered is not None and discovered.sha:
            content = discovered.founder_answer or _SAFE_REWRITE
        elif discovered is not None:
            content = unknown_lookup_reply(discovered.entity_key)
        else:
            content = _SAFE_REWRITE
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
