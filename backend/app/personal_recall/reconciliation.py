from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from app.concept_reconciliation.normalize import normalize_concept_text
from app.personal_recall.types import ContradictionCandidate, DecisionState, PersonalKnowledgeItem


def deduplicate(items: list[PersonalKnowledgeItem]) -> tuple[list[PersonalKnowledgeItem], dict[str, tuple[str, ...]]]:
    """Deduplicate only with strong identity evidence; equal text alone is insufficient."""
    kept: list[PersonalKnowledgeItem] = []
    duplicate_ids: dict[str, list[str]] = defaultdict(list)
    seen: dict[tuple[str, str], PersonalKnowledgeItem] = {}
    for item in items:
        key = (item.owner_id, item.content_hash or f"identity:{item.source_type.value}:{item.source_id}:{item.source_version or ''}")
        prior = seen.get(key)
        if prior is None:
            seen[key] = item
            kept.append(item)
        elif (
            item.content_hash
            and item.file_id is not None
            and item.file_id == prior.file_id
            and item.source_type == prior.source_type
            and item.metadata.get("content_hash_verified") is True
            and prior.metadata.get("content_hash_verified") is True
        ):
            duplicate_ids[prior.item_id].append(item.item_id)
        else:
            kept.append(item)
    return kept, {key: tuple(value) for key, value in duplicate_ids.items()}


def version_state(items: list[PersonalKnowledgeItem], *, now: datetime) -> tuple[list[str], list[str], list[str]]:
    ids = {item.item_id for item in items}
    superseded = {i.item_id for i in items if i.superseded_by or i.decision_state == DecisionState.SUPERSEDED}
    warnings: list[str] = []
    # Relationship edges use outgoing "supersedes": newer -> older.
    for item in items:
        superseded.update(x for x in item.relationship_edges.get("supersedes", ()) if x in ids)
        if item.superseded_by and item.superseded_by not in ids:
            warnings.append(f"{item.item_id}: supersession target {item.superseded_by} is missing")
    if _has_supersession_cycle(items):
        warnings.append("supersession graph contains a cycle; no cyclic item is current")
        superseded.update(_cyclic_nodes(items))
    historical = sorted(superseded)
    current = [i.item_id for i in items if i.item_id not in superseded and (i.valid_until is None or _aware(i.valid_until) > _aware(now)) and (i.valid_from is None or _aware(i.valid_from) <= _aware(now))]
    return current, historical, warnings


def _successors(item: PersonalKnowledgeItem) -> tuple[str, ...]:
    values = list(item.relationship_edges.get("supersedes", ()))
    if item.superseded_by:
        values.append(item.superseded_by)
    return tuple(values)


def _cyclic_nodes(items: list[PersonalKnowledgeItem]) -> set[str]:
    ids = {i.item_id for i in items}
    cyclic: set[str] = set()
    for start in ids:
        stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
        while stack:
            node, path = stack.pop()
            current = next((i for i in items if i.item_id == node), None)
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


def _has_supersession_cycle(items: list[PersonalKnowledgeItem]) -> bool:
    return bool(_cyclic_nodes(items))


def contradiction_pairs(items: list[PersonalKnowledgeItem]) -> list[tuple[str, str]]:
    ids = {i.item_id for i in items}
    pairs: set[tuple[str, str]] = set()
    for item in items:
        for other in item.relationship_edges.get("contradicts", ()):
            if other in ids and other != item.item_id:
                pairs.add(tuple(sorted((item.item_id, other))))
    return sorted(pairs)


def contradiction_candidates(items: list[PersonalKnowledgeItem], *, max_candidates: int = 100) -> list[ContradictionCandidate]:
    """Bounded candidates from structured propositions; never declares a contradiction."""
    grouped: dict[tuple[str, str], list[PersonalKnowledgeItem]] = defaultdict(list)
    for item in items:
        proposition = normalize_concept_text(str(item.metadata.get("proposition", "")))
        subject = normalize_concept_text(item.subject or "")
        if proposition and subject and "proposition_value" in item.metadata:
            grouped[(subject, proposition)].append(item)
    found: list[ContradictionCandidate] = []
    for (_subject, proposition), group in grouped.items():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if left.metadata["proposition_value"] == right.metadata["proposition_value"]:
                    continue
                found.append(ContradictionCandidate(left.item_id, right.item_id, proposition, "same structured proposition has incompatible values", 0.8))
                if len(found) >= max_candidates:
                    return found
    return found


def cluster_items(items: list[PersonalKnowledgeItem]) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = defaultdict(list)
    for item in items:
        key = normalize_concept_text(item.subject or item.topic or "unclassified")
        clusters[key or "unclassified"].append(item.item_id)
    for ids in clusters.values():
        ids.sort(key=lambda iid: next((i.created_at for i in items if i.item_id == iid), None) or _MIN)
    return dict(clusters)


_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
