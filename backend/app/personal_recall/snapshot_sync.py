from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.personal_recall.adapters import PersonalSourceAdapter, collect_owner_items
from app.personal_recall.index import LocalRecallIndex, RecallIndexError
from app.personal_recall.types import IndexState, SourceType


@dataclass(frozen=True)
class SnapshotSyncResult:
    discovered: int
    updated: int
    tombstoned: tuple[str, ...]


def synchronize_authoritative_sources(
    index: LocalRecallIndex,
    adapters: Iterable[PersonalSourceAdapter],
    *,
    owner_id: str,
    authoritative_source_types: Iterable[SourceType],
    max_items: int = 10_000,
) -> SnapshotSyncResult:
    """Refresh selected source classes and tombstone canonical deletions.

    Absence means deletion only after every adapter completed without truncation, leakage, or
    error and explicitly declared coverage for every authoritative source type.
    """
    if index.owner_id != owner_id:
        raise RecallIndexError("snapshot sync owner mismatch")
    authoritative = frozenset(authoritative_source_types)
    items, warnings, searched, failed, truncated = collect_owner_items(adapters, owner_id=owner_id, max_items=max_items)
    missing_coverage = authoritative - searched
    if warnings or failed or truncated or missing_coverage:
        raise RecallIndexError("authoritative snapshot sync is incomplete; refusing tombstone inference")
    discovered = {item.item_id: item for item in items if item.source_type in authoritative}
    tombstoned: list[str] = []
    merged = dict(index.items)
    for item_id, prior in tuple(merged.items()):
        if prior.source_type in authoritative and item_id not in discovered:
            prior.index_state = IndexState.DELETED
            prior.text = None
            prior.claims = ()
            prior.aliases = ()
            prior.metadata = {"tombstone_reason": "missing_from_complete_canonical_refresh"}
            tombstoned.append(item_id)
    merged.update(discovered)
    index.replace(list(merged.values()))
    return SnapshotSyncResult(len(discovered), len(discovered), tuple(sorted(tombstoned)))
