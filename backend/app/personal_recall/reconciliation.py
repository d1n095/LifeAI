from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from app.concept_reconciliation.normalize import normalize_concept_text
from app.personal_recall.types import DecisionState, PersonalKnowledgeItem


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
        elif item.content_hash and (item.file_id == prior.file_id or item.metadata.get("same_content") is True):
            duplicate_ids[prior.item_id].append(item.item_id)
        else:
            kept.append(item)
    return kept, {key: tuple(value) for key, value in duplicate_ids.items()}


def version_state(items: list[PersonalKnowledgeItem]) -> tuple[list[str], list[str]]:
    superseded = {i.item_id for i in items if i.superseded_by or i.decision_state == DecisionState.SUPERSEDED}
    # Relationship edges use outgoing "supersedes": newer -> older.
    for item in items:
        superseded.update(item.relationship_edges.get("supersedes", ()))
    historical = sorted(superseded)
    current = [i.item_id for i in items if i.item_id not in superseded and not i.valid_until]
    return current, historical


def contradiction_pairs(items: list[PersonalKnowledgeItem]) -> list[tuple[str, str]]:
    ids = {i.item_id for i in items}
    pairs: set[tuple[str, str]] = set()
    for item in items:
        for other in item.relationship_edges.get("contradicts", ()):
            if other in ids and other != item.item_id:
                pairs.add(tuple(sorted((item.item_id, other))))
    return sorted(pairs)


def cluster_items(items: list[PersonalKnowledgeItem]) -> dict[str, list[str]]:
    clusters: dict[str, list[str]] = defaultdict(list)
    for item in items:
        key = normalize_concept_text(item.subject or item.topic or "unclassified")
        clusters[key or "unclassified"].append(item.item_id)
    for ids in clusters.values():
        ids.sort(key=lambda iid: next((i.created_at for i in items if i.item_id == iid), None) or _MIN)
    return dict(clusters)


_MIN = datetime.min.replace(tzinfo=timezone.utc)
