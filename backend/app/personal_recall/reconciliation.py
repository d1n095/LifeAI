"""Bounded canonical-versus-projection repair primitives."""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timezone
from app.concept_reconciliation.normalize import normalize_concept_text
from app.personal_recall.types import ContradictionCandidate, DecisionState
from typing import Callable

from app.personal_recall.workers import ChangeKind, RecallIndexWorker, SourceChange


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int
    repaired: int
    stale: int
    orphaned: int
    version_conflicts: int


def deduplicate(items):
    kept, duplicates, seen = [], defaultdict(list), {}
    for item in items:
        key = (item.owner_id, item.content_hash or f"identity:{item.source_type.value}:{item.source_id}:{item.source_version or ''}")
        prior = seen.get(key)
        if prior is None:
            seen[key] = item
            kept.append(item)
        elif (item.content_hash and item.file_id is not None and item.file_id == prior.file_id
              and item.source_type == prior.source_type
              and item.metadata.get("content_hash_verified") is True
              and prior.metadata.get("content_hash_verified") is True):
            duplicates[prior.item_id].append(item.item_id)
        else:
            kept.append(item)
    return kept, {key: tuple(value) for key, value in duplicates.items()}


def version_state(items, *, now):
    ids = {item.item_id for item in items}
    superseded = {item.item_id for item in items if item.superseded_by or item.decision_state == DecisionState.SUPERSEDED}
    warnings = []
    for item in items:
        superseded.update(value for value in item.relationship_edges.get("supersedes", ()) if value in ids)
        if item.superseded_by and item.superseded_by not in ids:
            warnings.append(f"{item.item_id}: supersession target {item.superseded_by} is missing")
    if _has_supersession_cycle(items):
        warnings.append("supersession graph contains a cycle; no cyclic item is current")
        superseded.update(_cyclic_nodes(items))
    historical = sorted(superseded)
    current = [item.item_id for item in items if item.item_id not in superseded and (item.valid_until is None or _aware(item.valid_until) > _aware(now)) and (item.valid_from is None or _aware(item.valid_from) <= _aware(now))]
    return current, historical, warnings


def _successors(item):
    values = list(item.relationship_edges.get("supersedes", ()))
    if item.superseded_by:
        values.append(item.superseded_by)
    return tuple(values)


def _cyclic_nodes(items):
    ids = {item.item_id for item in items}
    cyclic = set()
    for start in ids:
        stack = [(start, (start,))]
        while stack:
            node, path = stack.pop()
            current = next((item for item in items if item.item_id == node), None)
            if current is None:
                continue
            for nxt in _successors(current):
                if nxt not in ids:
                    continue
                if nxt in path:
                    cyclic.update(path[path.index(nxt):])
                elif len(path) <= len(ids):
                    stack.append((nxt, (*path, nxt)))
    return cyclic


def _has_supersession_cycle(items):
    return bool(_cyclic_nodes(items))


def contradiction_pairs(items):
    ids = {item.item_id for item in items}
    pairs = set()
    for item in items:
        for other in item.relationship_edges.get("contradicts", ()):
            if other in ids and other != item.item_id:
                pairs.add(tuple(sorted((item.item_id, other))))
    return sorted(pairs)


def contradiction_candidates(items, *, max_candidates=100):
    grouped = defaultdict(list)
    for item in items:
        proposition = normalize_concept_text(str(item.metadata.get("proposition", "")))
        subject = normalize_concept_text(item.subject or "")
        if proposition and subject and "proposition_value" in item.metadata:
            grouped[(subject, proposition)].append(item)
    found = []
    for (_subject, proposition), group in grouped.items():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if left.metadata["proposition_value"] != right.metadata["proposition_value"]:
                    found.append(ContradictionCandidate(left.item_id, right.item_id, proposition, "same structured proposition has incompatible values", 0.8))
                    if len(found) >= max_candidates:
                        return found
    return found


def cluster_items(items):
    clusters = defaultdict(list)
    for item in items:
        clusters[normalize_concept_text(item.subject or item.topic or "unclassified") or "unclassified"].append(item.item_id)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for identifiers in clusters.values():
        identifiers.sort(key=lambda value: next((item.created_at for item in items if item.item_id == value), None) or minimum)
    return dict(clusters)


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reconcile_sources(*, worker: RecallIndexWorker, sources: list[tuple[str, str, int]],
                      load_generation: Callable[..., int | None], limit: int = 100) -> ReconciliationResult:
    """Repair only an explicit bounded source list; never performs an implicit full scan.

    `load_generation` must run in a fresh owner-scoped canonical transaction. A missing source
    becomes a delete event. Generation comparisons are monotonic and therefore safe against
    late delivery and delete/update reordering.
    """
    if not 1 <= limit <= 1_000 or len(sources) > limit:
        raise ValueError("reconciliation requires an explicit bounded source list")
    repaired = stale = orphaned = conflicts = 0
    for event_id, source_id, generation in sources:
        actual = load_generation(source_id=source_id)
        if actual is None:
            orphaned += 1
            worker.enqueue(SourceChange(event_id, worker.owner_id, source_id, ChangeKind.DELETED_DOCUMENT, generation, "deleted"))
            worker.run_once()
            repaired += 1
        elif actual != generation:
            stale += 1
            if actual < generation:
                conflicts += 1
            worker.enqueue(SourceChange(event_id, worker.owner_id, source_id, ChangeKind.EDITED_MESSAGE, actual, "updated"))
            worker.run_once()
            repaired += 1
    return ReconciliationResult(len(sources), repaired, stale, orphaned, conflicts)
