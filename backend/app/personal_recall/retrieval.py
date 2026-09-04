from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.concept_reconciliation.normalize import normalize_concept_text, token_set
from app.personal_recall.adapters import PersonalSourceAdapter, collect_owner_items
from app.personal_recall.query import understand_query
from app.personal_recall.reconciliation import cluster_items, contradiction_pairs, deduplicate, version_state
from app.personal_recall.types import IndexState, PersonalKnowledgeItem, RecallQuery, RecallResponse, RetrievalResult

SemanticScorer = Callable[[RecallQuery, PersonalKnowledgeItem], float]


class PersonalRecallEngine:
    """Hybrid, inspectable retrieval. No network calls and no telemetry."""

    def __init__(self, adapters: Iterable[PersonalSourceAdapter], *, aliases: dict[str, tuple[str, ...]] | None = None, semantic_scorer: SemanticScorer | None = None):
        self.adapters = list(adapters)
        self.aliases = aliases or {}
        self.semantic_scorer = semantic_scorer

    def recall(self, *, owner_id: str, raw_query: str, now: datetime | None = None) -> RecallResponse:
        query = understand_query(raw_query, known_aliases=self.aliases)
        items, warnings = collect_owner_items(self.adapters, owner_id=owner_id)
        items, duplicates = deduplicate(items)
        searchable: list[PersonalKnowledgeItem] = []
        for item in items:
            if item.index_state in (IndexState.DELETED, IndexState.FAILED):
                continue
            if item.index_state in (IndexState.PARTIAL, IndexState.STALE, IndexState.DISCOVERED, IndexState.PARSED):
                warnings.append(f"{item.item_id}: index is {item.index_state.value}; recall may be incomplete")
            if query.source_types and item.source_type not in query.source_types:
                continue
            if query.project_id and item.project_id != query.project_id:
                continue
            searchable.append(item)
        current, historical = version_state(searchable)
        ranked = [r for item in searchable if (r := self._score(query, item, now=now)) is not None]
        if query.current_only:
            ranked = [r for r in ranked if r.item.item_id in current]
        ranked.sort(key=lambda r: (r.relevance_score, _timestamp(r.item)), reverse=True)
        pairs = contradiction_pairs([r.item for r in ranked])
        by_id = {r.item.item_id: r for r in ranked}
        for left, right in pairs:
            if left in by_id:
                by_id[left].contradictions = tuple(sorted(set((*by_id[left].contradictions, right))))
            if right in by_id:
                by_id[right].contradictions = tuple(sorted(set((*by_id[right].contradictions, left))))
        for canonical, dupes in duplicates.items():
            if canonical in by_id:
                by_id[canonical].related_items = tuple(sorted(set((*by_id[canonical].related_items, *dupes))))
        matched_ids = {r.item.item_id for r in ranked}
        current = [i for i in current if i in matched_ids]
        historical = [i for i in historical if i in matched_ids]
        unresolved = sorted({i.item_id for i in searchable if i.verification_state.value in ("disputed", "unknown") and i.item_id in matched_ids} | {x for pair in pairs for x in pair})
        synthesis = self._synthesize(ranked, current, historical, pairs, warnings)
        return RecallResponse(query=query, results=ranked, clusters=cluster_items([r.item for r in ranked]), current_items=current, historical_items=historical, contradictions=pairs, unresolved=unresolved, index_warnings=sorted(set(warnings)), synthesis=synthesis)

    def _score(self, query: RecallQuery, item: PersonalKnowledgeItem, *, now: datetime | None) -> RetrievalResult | None:
        searchable = " ".join(filter(None, (item.subject, item.topic, item.text, *item.entities, *item.aliases, *item.claims)))
        norm = normalize_concept_text(searchable)
        tokens = token_set(searchable)
        variants = {normalize_concept_text(query.subject or ""), *(normalize_concept_text(a) for a in query.aliases), *query.terms}
        variants.discard("")
        exact_hits = [v for v in variants if v in norm]
        exact = min(1.0, len(exact_hits) / max(1, len(variants)))
        entity_values = {normalize_concept_text(x) for x in (item.subject, *item.entities, *item.aliases) if x}
        entity = 1.0 if variants & entity_values else 0.0
        fuzzy = max((SequenceMatcher(None, q, token).ratio() for q in variants for token in tokens), default=0.0)
        semantic = max(0.0, min(1.0, self.semantic_scorer(query, item))) if self.semantic_scorer else 0.0
        subject_match = bool(exact_hits or entity or fuzzy >= 0.78)
        # Semantic similarity is supporting evidence, never enough to cross subjects alone.
        if not subject_match:
            return None
        temporal = _temporal_score(item, now or datetime.now(timezone.utc))
        relevance = 0.35 * exact + 0.25 * entity + 0.15 * fuzzy + 0.15 * semantic + 0.10 * temporal
        why: list[str] = []
        if exact_hits:
            why.append("exact_term:" + ",".join(sorted(exact_hits)))
        if entity:
            why.append("entity_or_alias")
        if fuzzy >= 0.78 and not exact_hits:
            why.append(f"fuzzy_term:{fuzzy:.2f}")
        if semantic:
            why.append(f"semantic:{semantic:.2f}")
        if item.project_id and item.project_id == query.project_id:
            why.append("project_filter")
        return RetrievalResult(item=item, relevance_score=round(relevance, 6), exact_score=round(exact, 6), semantic_score=round(semantic, 6), entity_score=entity, temporal_score=round(temporal, 6), subject_match=True, why_matched=tuple(why), superseded_by=item.superseded_by, related_items=tuple(item.relationship_edges.get("related", ())))

    @staticmethod
    def _synthesize(results: list[RetrievalResult], current: list[str], historical: list[str], contradictions: list[tuple[str, str]], warnings: list[str]) -> str:
        sources = sorted({r.item.source_type.value for r in results})
        parts = [f"Found {len(results)} relevant item(s) across {len(sources)} source class(es)."]
        if historical:
            parts.append(f"{len(historical)} historical/superseded version(s); {len(current)} current candidate(s).")
        if contradictions:
            parts.append(f"{len(contradictions)} contradiction(s) remain unresolved; they were not silently merged.")
        if warnings:
            parts.append("Index coverage is incomplete; inspect warnings before treating this as 'all'.")
        return " ".join(parts)


def _timestamp(item: PersonalKnowledgeItem) -> float:
    value = item.updated_at or item.created_at or item.provenance.occurred_at
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _temporal_score(item: PersonalKnowledgeItem, now: datetime) -> float:
    value = item.updated_at or item.created_at or item.provenance.occurred_at
    if value is None:
        return 0.25
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - value).total_seconds() / 86400)
    return max(0.1, 1.0 / (1.0 + days / 365.0))
