"""MainAI Founder communication policy.

Factual accuracy over agreement. Explicit uncertainty. No fabricated progress.
No redundant messages. Concise answers to simple questions. Disagreement when
evidence contradicts the Founder. MainAI is the Founder's system, not a customer bot.
"""

from __future__ import annotations

import re
from typing import Any

from app.mainai_runtime_contract import sanitize_unverified_execution_claims

MAINAI_COMMUNICATION_POLICY = (
    "Kommunikationspolicy för MainAI (Founder-identitet, inte kund-AI):\n"
    "1. Saklig korrekthet går före medhåll. Håll inte med om evidensen motsäger grundaren.\n"
    "2. Markera osäkerhet explicit. Gissa inte status, kostnader, verifiering eller GO.\n"
    "3. Hitta aldrig på framsteg, bakgrundsarbete, minne som inte finns, eller avslutade förmågor.\n"
    "4. Upprepa inte samma svar. Om inget nytt finns, säg det kort.\n"
    "5. Svara kort på enkla frågor. Spara längd till evidens och osäkerhet.\n"
    "6. First-cycle GO är false tills transport/grants är sammansatta och oberoende granskade.\n"
    "7. Du är MainAI för grundaren. Blanda inte ihop det med kundvända AI-system."
)

_REDUNDANT_REPLY = "Jag har inget nytt att tillägga."
_GO_DISAGREEMENT_SV = (
    "Nej. First-cycle GO är false. Evidensen motsäger påståendet att systemet är redo att gå."
)
_WRITE_DISAGREEMENT_SV = (
    "Nej. GitHub-skrivning och privilegierade fjärreffekter är avstängda i den här profilen."
)
_RECALL_DISAGREEMENT_SV = (
    "Nej. Personal Recall HTTP är inte monterad och inte aktiverad i den här profilen."
)
_VERIFIED_DISAGREEMENT_SV = (
    "Nej. Självständig verifiering kräver hållbar evidens från någon annan än den som byggde arbetet."
)
_EVIDENCE_UNAVAILABLE_SV = (
    "Osäker: live evidens kunde inte läsas. Ingen driftsstatus påstås."
)

_FILLER_RE = re.compile(
    r"^\s*((självklart|absolut|precis så|jag håller med|great question|of course|sure!?)"
    r"[.!]?\s*)+",
    re.IGNORECASE,
)
_SIMPLE_QUESTION_RE = re.compile(r"^\s*[^?]{0,80}\?\s*$", re.UNICODE)
_GO_CLAIM_RE = re.compile(
    r"("
    r"\bgo\s*(är|=|is)\s*true\b|"
    r"\bfirst[-\s]?cycle[-\s]?go\s*(är|=|is)\s*true\b|"
    r"\ballt är (klart|redo|operationellt)\b|"
    r"\bsystemet är (klart|redo|igång|operationellt)\b|"
    r"\bwe are good to go\b"
    r")",
    re.IGNORECASE,
)
_WRITE_CLAIM_RE = re.compile(
    r"\b(github[- ]skrivning är (på|aktiv)|privileged remote effects (are )?on|"
    r"vi skriver till github)\b",
    re.IGNORECASE,
)
_RECALL_CLAIM_RE = re.compile(r"\b(recall är (på|aktiv|monterad)|recall is (on|mounted|active))\b", re.IGNORECASE)
_VERIFIED_CLAIM_RE = re.compile(
    r"\b(självständigt verifierad|independently verified|all work is verified)\b",
    re.IGNORECASE,
)
_ASSERTION_RE = re.compile(
    r"("
    r"\b(är|=|is)\s*true\b|"
    r"\ballt är (klart|redo|operationellt)\b|"
    r"\bsystemet är (klart|redo|igång|operationellt)\b|"
    r"\bwe are good to go\b|"
    r"\bgithub[- ]skrivning är (på|aktiv)\b|"
    r"\bprivileged remote effects (are )?on\b|"
    r"\bvi skriver till github\b|"
    r"\brecall är (på|aktiv|monterad)\b|"
    r"\brecall is (on|mounted|active)\b|"
    r"\bsjälvständigt verifierad\b|"
    r"\bindependently verified\b|"
    r"\ball work is verified\b"
    r")",
    re.IGNORECASE,
)
_UNCERTAINTY_RE = re.compile(r"\b(osäker|vet inte|okänd|unknown|uncertain)\b", re.IGNORECASE)


def is_simple_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or "\n" in stripped:
        return False
    return bool(_SIMPLE_QUESTION_RE.match(stripped))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _is_state_assertion(text: str) -> bool:
    """Questions are not claims. A simple question without an assertive clause is ignored."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if is_simple_question(stripped) and not _ASSERTION_RE.search(stripped):
        return False
    return True


def contradiction_notices(*, founder_message: str | None, evidence: dict[str, Any] | None) -> list[str]:
    """When the Founder's wording contradicts durable evidence, disagree explicitly."""
    if not founder_message:
        return []
    evidence = evidence or {}
    if evidence.get("evidence_unavailable"):
        return []
    if not _is_state_assertion(founder_message):
        return []
    notices: list[str] = []
    if _GO_CLAIM_RE.search(founder_message) and evidence.get("first_cycle_go") is not True:
        notices.append(_GO_DISAGREEMENT_SV)
    if _WRITE_CLAIM_RE.search(founder_message) and evidence.get("github_write_enabled") is not True:
        notices.append(_WRITE_DISAGREEMENT_SV)
    if _RECALL_CLAIM_RE.search(founder_message) and evidence.get("recall_http_mounted") is not True:
        notices.append(_RECALL_DISAGREEMENT_SV)
    if _VERIFIED_CLAIM_RE.search(founder_message) and evidence.get("independently_verified") is False:
        notices.append(_VERIFIED_DISAGREEMENT_SV)
    return notices


def apply_communication_policy(
    message: str,
    *,
    founder_message: str | None = None,
    prior_assistant: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Sanitize, drop redundant copies, disagree with contradicted claims, keep simple answers short."""
    sanitized = sanitize_unverified_execution_claims(message or "")
    notices = contradiction_notices(founder_message=founder_message, evidence=evidence)

    if prior_assistant and _normalize(sanitized) and _normalize(sanitized) == _normalize(prior_assistant):
        body = _REDUNDANT_REPLY
    else:
        body = sanitized
        if founder_message and is_simple_question(founder_message):
            body = _FILLER_RE.sub("", body).strip() or body

    if notices:
        # Disagreement is not optional when evidence contradicts the Founder.
        prefix = " ".join(notices)
        if _normalize(prefix) in _normalize(body):
            return body
        return f"{prefix} {body}".strip()

    evidence = evidence or {}
    if evidence.get("evidence_unavailable") and _EVIDENCE_UNAVAILABLE_SV not in body:
        body = f"{_EVIDENCE_UNAVAILABLE_SV} {body}".strip()
    if evidence.get("usage_cost_usd") is None and "USD" in body and not _UNCERTAINTY_RE.search(body):
        if re.search(r"\b(kostnad|spend|cost)\b", body, re.IGNORECASE):
            body = f"{body} Kostnaden är okänd där pris saknas.".strip()
    return body
