"""Inbound founder-utterance classification.

Capability/risk classification gates authority-bearing requests. Lookup intent is
detected from ask-to-fetch context, not from the mere presence of GitHub/CI words.
English and Swedish.
"""

from __future__ import annotations

import re

from app.mainai_continuous_conversation.entities import classify_requested_entity
from app.mainai_continuous_conversation.types import InboundClassification, InboundKind, InterruptKind, RelayCategory

_SWEDISH = re.compile(
    r"[åäöÅÄÖ]|(radera|grenen|lansera|s[äa]kerhetspolicyn|godk[äa]nn|k[öo]pet|klistra|gr[öo]nt|vad sa|fakturan|databasen|leverant)",
    re.IGNORECASE,
)
_HOW_TO = re.compile(
    r"\b(how (do i|can i|to)|hur (g[öo]r|kan) (jag|man)|you can configure|your ci status page)\b",
    re.IGNORECASE,
)
_IMMEDIATE_ACTION = re.compile(
    r"("
    r"\b(now|right now|please (do|activate|enable|delete|drop|release|deploy)|go ahead|do it)\b"
    r"|just nu|\bnu\b|\bk[öo]r\b|\baktivera\b|\bsl[åa] p[åa]\b"
    r")",
    re.IGNORECASE,
)

_DESTRUCTIVE_VERB = re.compile(
    r"("
    r"\b(delete|drop|wipe|destroy|remove|erase|truncate|purge|radera|avveckla|t[öo]m(?:ma)?|rensa)\b"
    r"|ta bort|rm\s+-rf"
    r")",
    re.IGNORECASE,
)
_DESTRUCTIVE_OBJECT = re.compile(
    r"("
    r"\b(database|databas(?:en)?|db|postgres|postgresql|table|tabellen|schema|"
    r"branch|gren(?:en)?|production|prod(?:uktion)?|prod data|volume|disk)\b"
    r")",
    re.IGNORECASE,
)
_SPEND_VERB = re.compile(
    r"\b(pay|approve|authorize|spend|purchase|betala|godk[äa]nn)\b",
    re.IGNORECASE,
)
_SPEND_OBJECT = re.compile(
    r"\b(invoice|faktura(?:n)?|budget|k[öo]p(?:et)?|charge)\b|\$\s*\d+|\d[\d\s]*kr\b",
    re.IGNORECASE,
)
_DEPLOY = re.compile(
    r"("
    r"\b(deploy(?:ing)? to prod(?:uction)?|production deploy|deploya till prod|"
    r"sl[äa]pp till prod(?:uktion)?|release to production|ship to prod(?:uction)?|"
    r"go live|push (this |it )?live|cut over to prod|publicera till produktion|g[åa] live)\b"
    r")",
    re.IGNORECASE,
)
_RECALL_ACTIVATION = re.compile(
    r"("
    r"\b((activate|enable|turn on|switch on|start) (personal )?recall|"
    r"(activate|enable|turn on) (the )?personal (recall|memory)|"
    r"production recall|recall in production)\b"
    r"|aktivera (personal )?recall|aktivera minnet|sl[åa] p[åa] (personal )?recall|"
    r"aktivera personligt minne"
    r")",
    re.IGNORECASE,
)
_POLICY = re.compile(
    r"("
    r"\b(security policy|rls|secret|credential|force[- ]push|rotate (the )?keys?)\b"
    r"|s[äa]kerhetspolicyn|[äa]ndra s[äa]kerhet"
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

_SHA_LOOKUP = re.compile(
    r"("
    r"\b(sha|commit hash|tree hash|tree id|git rev-parse|commit id|commit-id|latest commit|"
    r"what hash|hash (is )?(on|of) the remote|remote (tip|sha|hash)|latest commit on the remote)\b"
    r"|sha:n|klistra in sha|drop the latest commit|commit hash in chat"
    r"|could you share the commit"
    r"|vad [äa]r .*sha"
    r"|senaste (commit(?:ten)?|ändringen|sha)"
    r")",
    re.IGNORECASE,
)
_BRANCH_LOOKUP = re.compile(
    r"\b(branch name|which branch|working branch|remote branch|vilken gren|branchnamn)\b",
    re.IGNORECASE,
)
_TEST_LOOKUP = re.compile(
    r"("
    r"\b(test results?|pytest|ci (status|state|green|red|pass|passed)|github actions?|"
    r"ci gick igenom|did (the )?tests pass|gick testerna igenom)\b"
    r"|[äa]r ci gr[öo]nt|ci gr[öo]nt|testresultaten|kolla om ci"
    r")",
    re.IGNORECASE,
)
_FACT_ASK = re.compile(
    r"("
    r"\b(what (is|hash|sha)|what's|whats)\b"
    r"|vad [äa]r"
    r")",
    re.IGNORECASE,
)
_AGENT_LOOKUP = re.compile(
    r"("
    r"\b(tell (claude|cursor|codex)|ask (claude|cursor|codex)|relay|paste (this|the sha|it) (to|into))\b"
    r"|vad sa (cursor|claude|codex)"
    r"|what did (cursor|claude|codex) (report|say|write|find)"
    r"|vad rapporterade (cursor|claude|codex)"
    r"|vad kom (cursor|claude|codex) fram"
    r"|har (cursor|claude|codex) (pushat|pushed|pushat senaste)"
    r")",
    re.IGNORECASE,
)
_WAIT_GRAPH = re.compile(
    r"\b(who is waiting|who should work next|next agent|blocked by whom|vem v[äa]ntar)\b",
    re.IGNORECASE,
)
_FETCH_ASK = re.compile(
    r"("
    r"\b(can you|could you|please|look up|check|drop|send|share|give me|kolla|skicka|klistra|h[äa]mta)\b"
    r"|kan du"
    r")",
    re.IGNORECASE,
)


def _language(text: str) -> str:
    return "sv" if _SWEDISH.search(text) else "en"


def classify_capability_risk(text: str) -> InterruptKind:
    """Classify authority-bearing capability/risk. Phrase lists are not the authority model."""

    educational_only = bool(_HOW_TO.search(text)) and not _IMMEDIATE_ACTION.search(text)
    if educational_only:
        return InterruptKind.NONE
    if _RECALL_ACTIVATION.search(text):
        return InterruptKind.FOUNDER_ONLY_AUTHORITY
    if _DESTRUCTIVE_VERB.search(text) and _DESTRUCTIVE_OBJECT.search(text):
        return InterruptKind.DESTRUCTIVE_ACTION
    if _DESTRUCTIVE_VERB.search(text) and re.search(r"\b(branch|gren)\b", text, re.IGNORECASE):
        return InterruptKind.DESTRUCTIVE_ACTION
    if _DEPLOY.search(text):
        return InterruptKind.DESTRUCTIVE_ACTION
    if _POLICY.search(text):
        return InterruptKind.SECURITY_POLICY
    if _SPEND_VERB.search(text) and _SPEND_OBJECT.search(text):
        return InterruptKind.MONEY_BUDGET
    if _SPEND_OBJECT.search(text) and re.search(r"\b(4000|invoice|faktura)\b", text, re.IGNORECASE):
        return InterruptKind.MONEY_BUDGET
    if _PRODUCT.search(text):
        return InterruptKind.MATERIAL_PRODUCT_DECISION
    return InterruptKind.NONE


def classify_inbound(text: str) -> InboundClassification:
    lowered = text.strip()
    if not lowered:
        return InboundClassification(InboundKind.ORDINARY_CHAT, reason="empty")

    language = _language(lowered)
    interrupt = classify_capability_risk(lowered)

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
            InterruptKind.FOUNDER_ONLY_AUTHORITY: (
                "Grundarauktoritet krävs: Recall-aktivering utförs inte av den här ytan."
                if language == "sv"
                else "Founder authority required: Recall activation will not be executed on this surface."
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
    if _SHA_LOOKUP.search(lowered) and (
        _FETCH_ASK.search(lowered)
        or _FACT_ASK.search(lowered)
        or classify_requested_entity(lowered)
        or "sha" in lowered.lower()
    ):
        categories.append(RelayCategory.SHA)
    if _BRANCH_LOOKUP.search(lowered):
        categories.append(RelayCategory.BRANCH_NAME)
    _ci_status_question = re.search(
        r"([äa]r ci gr[öo]nt|ci gr[öo]nt|\bis ci green\b|\bci status\b|ci gick igenom|testresultaten|did (the )?tests pass)",
        lowered,
        re.IGNORECASE,
    )
    if _TEST_LOOKUP.search(lowered) and (_FETCH_ASK.search(lowered) or _FACT_ASK.search(lowered) or _ci_status_question):
        categories.append(RelayCategory.TEST_RESULT)
        categories.append(RelayCategory.CI_STATE)
    if _AGENT_LOOKUP.search(lowered):
        categories.append(RelayCategory.AGENT_MESSAGE)
        categories.append(RelayCategory.NEXT_AGENT)
    if _WAIT_GRAPH.search(lowered):
        categories.append(RelayCategory.WAIT_GRAPH)
        categories.append(RelayCategory.NEXT_AGENT)
    if (_FETCH_ASK.search(lowered) or _FACT_ASK.search(lowered)) and (
        _SHA_LOOKUP.search(lowered) or _TEST_LOOKUP.search(lowered) or _AGENT_LOOKUP.search(lowered)
    ):
        if RelayCategory.SHA not in categories and _SHA_LOOKUP.search(lowered):
            categories.append(RelayCategory.SHA)
        if RelayCategory.TEST_RESULT not in categories and _TEST_LOOKUP.search(lowered):
            categories.append(RelayCategory.TEST_RESULT)
            categories.append(RelayCategory.CI_STATE)

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
