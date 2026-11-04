"""Indexing & Retrieval Efficiency. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

Improves retrieval so MainAI does not repeatedly scan entire history. Pure: reasons over
caller-supplied retrieval events and index metadata; owns no index storage itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IndexDefinition:
    index_name: str
    keyed_on: str  # e.g. "program", "sha", "branch", "actor", "error_signature"
    target_kind: str


@dataclass(frozen=True)
class RetrievalEvent:
    query: str
    index_used: str | None
    hit: bool
    false_positive: bool = False


@dataclass(frozen=True)
class RetrievalEffectiveness:
    total_queries: int
    hit_rate: float | None
    false_positive_rate: float | None


def assess_retrieval_effectiveness(events: tuple[RetrievalEvent, ...]) -> RetrievalEffectiveness:
    if not events:
        return RetrievalEffectiveness(total_queries=0, hit_rate=None, false_positive_rate=None)
    hits = sum(1 for e in events if e.hit)
    false_positives = sum(1 for e in events if e.false_positive)
    return RetrievalEffectiveness(total_queries=len(events), hit_rate=hits / len(events), false_positive_rate=false_positives / len(events))


def detect_stale_index(*, index_last_verified_at: datetime, source_last_changed_at: datetime) -> bool:
    """True when the underlying source has changed more recently than the index was last
    verified -- the index cannot be trusted current until re-verified."""

    return source_last_changed_at > index_last_verified_at
