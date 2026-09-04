from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.concept_reconciliation.normalize import normalize_concept_text, token_set
from app.personal_recall.adapters import PersonalSourceAdapter, collect_owner_items
from app.personal_recall.query import understand_query
from app.personal_recall.reconciliation import cluster_items, contradiction_candidates, contradiction_pairs, deduplicate, version_state
from app.personal_recall.types import AliasBinding, CompletenessState, CoverageReport, DecisionState, IndexState, PersonalKnowledgeItem, RecallQuery, RecallResponse, RetrievalResult, SourceAuthority, SourceType, VerificationState


class LocalSemanticScorer:
    """Marker base for in-process/local scorers. Network/provider scorers are rejected."""

    is_local_personal_recall_scorer = True

    def score(self, query: RecallQuery, item: PersonalKnowledgeItem) -> float:
        raise NotImplementedError


class PersonalRecallEngine:
    """Hybrid, inspectable retrieval. No network calls and no telemetry."""

    MAX_ITEMS = 10_000
    MAX_RESULTS = 500
    MAX_TEXT_CHARS = 100_000
    MAX_ALIASES_PER_ITEM = 256

    def __init__(
        self,
        adapters: Iterable[PersonalSourceAdapter],
        *,
        alias_bindings: Iterable[AliasBinding] = (),
        semantic_scorer: LocalSemanticScorer | None = None,
        expected_source_types: Iterable[SourceType] = (),
    ):
        self.adapters = list(adapters)
        self.alias_bindings = tuple(alias_bindings)
        if len(self.alias_bindings) > 10_000:
            raise ValueError("alias registry exceeds safe bound")
        if semantic_scorer is not None and not getattr(semantic_scorer, "is_local_personal_recall_scorer", False):
            raise ValueError("semantic scorer must be explicitly local")
        self.semantic_scorer = semantic_scorer
        self.expected_source_types = frozenset(expected_source_types)

    def recall(self, *, owner_id: str, raw_query: str, now: datetime | None = None, project_id: str | None = None, domain: str | None = None) -> RecallResponse:
        now = now or datetime.now(timezone.utc)
        query = understand_query(raw_query, alias_bindings=self.alias_bindings, owner_id=owner_id, project_id=project_id, domain=domain, now=now)
        items, warnings, searched_types, failed_adapters, truncated = collect_owner_items(self.adapters, owner_id=owner_id, max_items=self.MAX_ITEMS)
        items, duplicates = deduplicate(items)
        searchable: list[PersonalKnowledgeItem] = []
        for item in items:
            if item.index_state in (IndexState.DELETED, IndexState.FAILED):
                continue
            if item.index_state in (IndexState.PARTIAL, IndexState.STALE, IndexState.DISCOVERED, IndexState.PARSED):
                warnings.append(f"{item.item_id}: index is {item.index_state.value}; recall may be incomplete")
            if item.created_at and _as_aware(item.created_at) > _as_aware(now):
                warnings.append(f"{item.item_id}: future-dated source is not evidence of current truth")
            if query.source_types and item.source_type not in query.source_types:
                continue
            if query.project_id and item.project_id != query.project_id:
                continue
            searchable.append(item)
        current, historical, temporal_warnings = version_state(searchable, now=now)
        warnings.extend(temporal_warnings)
        ranked = [r for item in searchable if (r := self._score(query, item, now=now)) is not None]
        if query.current_only:
            ranked = [r for r in ranked if r.item.item_id in current]
        ranked.sort(key=lambda r: (r.relevance_score, _timestamp(r.item)), reverse=True)
        if len(ranked) > self.MAX_RESULTS:
            ranked = ranked[: self.MAX_RESULTS]
            warnings.append(f"results bounded at {self.MAX_RESULTS}")
            truncated = True
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
        candidates = contradiction_candidates([r.item for r in ranked])
        unresolved = sorted({i.item_id for i in searchable if i.verification_state.value in ("disputed", "unknown") and i.item_id in matched_ids} | {x for pair in pairs for x in pair} | {x for c in candidates for x in (c.left_item_id, c.right_item_id)})
        _warn_multiple_current(searchable, current, warnings)
        missing = self.expected_source_types - searched_types
        if failed_adapters or truncated or any("partial" in w or "stale" in w for w in warnings):
            completeness = CompletenessState.KNOWN_PARTIAL
        elif self.expected_source_types and not missing:
            completeness = CompletenessState.COMPLETE
        else:
            completeness = CompletenessState.UNKNOWN
        coverage = CoverageReport(completeness, tuple(sorted(searched_types, key=lambda x: x.value)), tuple(sorted(missing, key=lambda x: x.value)), tuple(failed_adapters), truncated)
        synthesis = self._synthesize(ranked, current, historical, pairs, warnings, coverage)
        return RecallResponse(query=query, results=ranked, clusters=cluster_items([r.item for r in ranked]), current_items=current, historical_items=historical, contradictions=pairs, contradiction_candidates=candidates, unresolved=unresolved, index_warnings=sorted(set(warnings)), coverage=coverage, synthesis=synthesis)

    def _score(self, query: RecallQuery, item: PersonalKnowledgeItem, *, now: datetime | None) -> RetrievalResult | None:
        searchable = " ".join(filter(None, (item.subject, item.topic, (item.text or "")[: self.MAX_TEXT_CHARS], *item.entities, *item.aliases[: self.MAX_ALIASES_PER_ITEM], *item.claims)))
        tokens = token_set(searchable)
        variants = {normalize_concept_text(query.subject or ""), *(normalize_concept_text(a) for a in query.aliases), *query.terms}
        variants.discard("")
        exact_hits = [v for v in variants if set(v.split()).issubset(tokens)]
        exact = min(1.0, len(exact_hits) / max(1, len(variants)))
        entity_values = {normalize_concept_text(x) for x in (item.subject, *item.entities, *item.aliases) if x}
        entity = 1.0 if any(set(v.split()).issubset(set(e.split())) or set(e.split()).issubset(set(v.split())) for v in variants for e in entity_values) else 0.0
        fuzzy = max((SequenceMatcher(None, q, token).ratio() for q in variants for token in tokens if min(len(q), len(token)) >= 5), default=0.0)
        term_coverage = len({term for term in query.terms if term in tokens}) / max(1, len(query.terms))
        subject_match = bool(entity or term_coverage >= 0.6 or (len(query.terms) == 1 and (exact_hits or fuzzy >= 0.84)))
        # Semantic similarity is supporting evidence, never enough to cross subjects alone.
        if not subject_match:
            return None
        semantic = max(0.0, min(1.0, self.semantic_scorer.score(query, item))) if self.semantic_scorer else 0.0
        temporal = _temporal_score(item, now or datetime.now(timezone.utc))
        authority = _authority_score(item)
        truth = _truth_score(item)
        relevance = 0.25 * exact + 0.20 * entity + 0.10 * fuzzy + 0.10 * semantic + 0.05 * temporal + 0.15 * authority + 0.15 * truth
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
        return RetrievalResult(item=item, relevance_score=round(relevance, 6), exact_score=round(exact, 6), semantic_score=round(semantic, 6), entity_score=entity, temporal_score=round(temporal, 6), authority_score=authority, truth_score=truth, subject_match=True, why_matched=tuple(why), superseded_by=item.superseded_by, related_items=tuple(item.relationship_edges.get("related", ())))

    @staticmethod
    def _synthesize(results: list[RetrievalResult], current: list[str], historical: list[str], contradictions: list[tuple[str, str]], warnings: list[str], coverage: CoverageReport) -> str:
        sources = sorted({r.item.source_type.value for r in results})
        parts = [f"Found {len(results)} relevant item(s) across {len(sources)} source class(es). Coverage: {coverage.state.value}."]
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
    days = (now - value).total_seconds() / 86400
    if days < 0:
        return 0.0
    return max(0.1, 1.0 / (1.0 + days / 365.0))


def _authority_score(item: PersonalKnowledgeItem) -> float:
    return {SourceAuthority.PRIMARY: 1.0, SourceAuthority.USER: 0.9, SourceAuthority.VERIFIED_DERIVED: 0.75, SourceAuthority.DERIVED: 0.5, SourceAuthority.ASSISTANT: 0.3, SourceAuthority.UNKNOWN: 0.2}[item.source_authority]


def _truth_score(item: PersonalKnowledgeItem) -> float:
    verify = {VerificationState.VERIFIED: 1.0, VerificationState.UNVERIFIED: 0.45, VerificationState.UNKNOWN: 0.3, VerificationState.DISPUTED: 0.15, VerificationState.FAILED: 0.0}[item.verification_state]
    decision = {DecisionState.VERIFIED_RESULT: 1.0, DecisionState.DECISION: 0.9, DecisionState.IMPLEMENTATION: 0.8, DecisionState.PLAN: 0.65, DecisionState.CLAIM: 0.5, DecisionState.IDEA: 0.35, DecisionState.ASSUMPTION: 0.25, DecisionState.MENTION: 0.2, DecisionState.UNKNOWN: 0.2, DecisionState.REJECTED: 0.1, DecisionState.SUPERSEDED: 0.0}[item.decision_state]
    return round((verify + decision) / 2.0, 6)


def _warn_multiple_current(items: list[PersonalKnowledgeItem], current: list[str], warnings: list[str]) -> None:
    current_set = set(current)
    subjects: dict[str, list[str]] = {}
    for item in items:
        if item.item_id in current_set:
            subjects.setdefault(normalize_concept_text(item.subject or ""), []).append(item.item_id)
    for subject, ids in subjects.items():
        if subject and len(ids) > 1:
            warnings.append(f"{subject}: {len(ids)} concurrent current candidates require reconciliation")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
