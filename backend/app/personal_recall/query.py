from __future__ import annotations

import re

from app.concept_reconciliation.normalize import normalize_concept_text
from app.personal_recall.types import AliasBinding, AliasVerification, QueryIntent, RecallQuery, SourceType

_STOP = {"allt", "all", "allting", "om", "det", "detta", "här", "kan", "du", "kolla", "upp", "visa", "mig", "vad", "vi", "har", "jag", "tidigare", "för", "sedan", "the", "about", "den", "där", "nu", "igen", "vare", "var", "va", "fram", "ta", "haft", "snacka", "skicka", "förut", "sist"}


def _has(text: str, *phrases: str) -> bool:
    return any(p in text for p in phrases)


def understand_query(
    raw: str,
    *,
    alias_bindings: tuple[AliasBinding, ...] = (),
    owner_id: str | None = None,
    project_id: str | None = None,
    domain: str | None = None,
    now=None,
) -> RecallQuery:
    normalized = normalize_concept_text(raw)
    intents: list[QueryIntent] = []
    source_types: list[SourceType] = []
    if _has(normalized, "allt om", "all about", "ta fram allt", "allt vi haft"):
        intents.append(QueryIntent.BROAD_RECALL)
    if _has(normalized, "senaste", "aktuell", "current", "latest", "vilken va senaste"):
        intents.append(QueryIntent.LATEST_STATE)
    if _has(normalized, "tidslinje", "timeline", "över tid"):
        intents.append(QueryIntent.TIMELINE)
    if _has(normalized, "vad ändrade", "vad ändra", "ändringar", "change history"):
        intents.append(QueryIntent.CHANGE_HISTORY)
    if _has(normalized, "kom vi fram", "kom fram", "beslut", "bestämde", "decision"):
        intents.append(QueryIntent.DECISION_HISTORY)
    if _has(normalized, "varifrån", "källa", "source", "bevis"):
        intents.extend([QueryIntent.SOURCE_LOOKUP, QueryIntent.SHOW_EVIDENCE])
    if _has(normalized, "pdf", "pdfen", "fil", "dokument", "file"):
        intents.append(QueryIntent.FILE_LOOKUP)
        source_types.append(SourceType.FILE)
    if _has(normalized, "motsäg", "contradiction", "konflikt"):
        intents.append(QueryIntent.CONTRADICTION_SEARCH)
    if _has(normalized, "jämför", "gamla version", "compare"):
        intents.append(QueryIntent.VERSION_COMPARE)
    if _has(normalized, "varför", "why") and _has(normalized, "ändr", "tog vi bort", "removed"):
        intents.append(QueryIntent.WHY_CHANGED)
    if not intents:
        intents.append(QueryIntent.BROAD_RECALL)
    current_only = QueryIntent.LATEST_STATE in intents and QueryIntent.VERSION_COMPARE not in intents
    words = [w for w in re.findall(r"\w+", normalized) if len(w) > 1 and w not in _STOP]
    subject = " ".join(words) or None
    aliases: list[str] = []
    query_tokens = set(words)
    for binding in alias_bindings:
        if binding.verification != AliasVerification.VERIFIED or binding.owner_id != owner_id:
            continue
        if binding.project_id is not None and binding.project_id != project_id:
            continue
        if binding.domain is not None and binding.domain != domain:
            continue
        if binding.valid_from is not None and now is not None and binding.valid_from > now:
            continue
        if binding.valid_until is not None and now is not None and binding.valid_until <= now:
            continue
        alias_norm = normalize_concept_text(binding.alias)
        # Token/phrase boundary matching: "HAp" must not match "app" or a substring.
        if set(alias_norm.split()).issubset(query_tokens):
            subject = binding.canonical
            aliases.append(binding.alias)
    return RecallQuery(raw=raw, normalized=normalized, terms=tuple(dict.fromkeys(words)), intents=tuple(dict.fromkeys(intents)), subject=subject, aliases=tuple(dict.fromkeys(aliases)), project_id=project_id, source_types=tuple(source_types), current_only=current_only)
