from __future__ import annotations

import re

from app.concept_reconciliation.normalize import normalize_concept_text
from app.personal_recall.types import QueryIntent, RecallQuery, SourceType

_STOP = {"allt", "all", "allting", "om", "det", "detta", "här", "kan", "du", "kolla", "upp", "visa", "mig", "vad", "vi", "har", "jag", "tidigare", "för", "sedan", "the", "about"}


def _has(text: str, *phrases: str) -> bool:
    return any(p in text for p in phrases)


def understand_query(raw: str, *, known_aliases: dict[str, tuple[str, ...]] | None = None) -> RecallQuery:
    normalized = normalize_concept_text(raw)
    intents: list[QueryIntent] = []
    source_types: list[SourceType] = []
    if _has(normalized, "allt om", "all about"):
        intents.append(QueryIntent.BROAD_RECALL)
    if _has(normalized, "senaste", "aktuell", "current", "latest"):
        intents.append(QueryIntent.LATEST_STATE)
    if _has(normalized, "tidslinje", "timeline", "över tid"):
        intents.append(QueryIntent.TIMELINE)
    if _has(normalized, "vad ändrade", "ändringar", "change history"):
        intents.append(QueryIntent.CHANGE_HISTORY)
    if _has(normalized, "kom vi fram", "beslut", "bestämde", "decision"):
        intents.append(QueryIntent.DECISION_HISTORY)
    if _has(normalized, "varifrån", "källa", "source", "bevis"):
        intents.extend([QueryIntent.SOURCE_LOOKUP, QueryIntent.SHOW_EVIDENCE])
    if _has(normalized, "pdf", "fil", "dokument", "file"):
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
    if known_aliases:
        for canonical, variants in known_aliases.items():
            haystack = (canonical, *variants)
            if any(normalize_concept_text(v) in normalized for v in haystack):
                subject = canonical
                aliases.extend(v for v in haystack if v != canonical)
    return RecallQuery(raw=raw, normalized=normalized, terms=tuple(dict.fromkeys(words)), intents=tuple(dict.fromkeys(intents)), subject=subject, aliases=tuple(dict.fromkeys(aliases)), source_types=tuple(source_types), current_only=current_only)
