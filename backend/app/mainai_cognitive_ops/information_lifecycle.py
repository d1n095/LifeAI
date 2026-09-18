"""Information Lifecycle Engine -- fragmentation, defragmentation, deduplication. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

SUMMARY != SOURCE. RAW SOURCE != SUMMARY. ARCHIVED != FORGOTTEN. DEDUPLICATED != DELETED.
COMPRESSED != AUTHORITATIVE ORIGINAL. SIMILAR != SAME. DUPLICATE SOURCE != INDEPENDENT SOURCE.
DEDUPLICATED RECORD MUST RETAIN PROVENANCE TO ALL ORIGINAL OCCURRENCES.

Pure: reasons over caller-supplied information items, never a second copy of any durable
store owned by another package."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.mainai_cognitive_ops.types import InformationTier


@dataclass(frozen=True)
class InformationItem:
    item_id: str
    tier: InformationTier
    content_ref: str
    parent_id: str | None = None
    provenance: tuple[str, ...] = field(default_factory=tuple)


def fragment(*, source_id: str, unit_boundaries: tuple[str, ...]) -> tuple[InformationItem, ...]:
    """Splits `source_id` into one `InformationItem` per boundary, each pointing back at
    `source_id` via `parent_id` so the original can always be reconstructed -- fragmentation
    never discards the parent link."""

    return tuple(
        InformationItem(item_id=f"{source_id}#{i}", tier=InformationTier.RAW_SOURCE, content_ref=unit, parent_id=source_id, provenance=(source_id,))
        for i, unit in enumerate(unit_boundaries)
    )


@dataclass(frozen=True)
class DefragmentedBundle:
    """A logical VIEW over scattered fragments that belong together (e.g. one bug's issue,
    logs, commit, test, review, fix, re-review) -- never a merge/rewrite of the originals."""

    bundle_id: str
    member_item_ids: tuple[str, ...]
    reconstructable: bool


def defragment(*, bundle_id: str, related_items: tuple[InformationItem, ...]) -> DefragmentedBundle:
    reconstructable = all(item.parent_id is not None or item.provenance for item in related_items)
    return DefragmentedBundle(bundle_id=bundle_id, member_item_ids=tuple(i.item_id for i in related_items), reconstructable=reconstructable)


@dataclass(frozen=True)
class DeduplicationResult:
    kept_item_id: str
    duplicate_item_ids: tuple[str, ...]
    retained_provenance: tuple[str, ...]  # provenance to ALL original occurrences, never dropped


def deduplicate_exact(*, items: tuple[InformationItem, ...], key_fn) -> tuple[DeduplicationResult, ...]:
    """Groups items whose `key_fn(item)` is identical (exact duplicates only -- SIMILAR != SAME,
    this function never fuzzy-matches). The kept record's `retained_provenance` is the union of
    every group member's own provenance plus their item_ids, so no original occurrence is lost
    even though only one representative row remains "active"."""

    groups: dict[object, list[InformationItem]] = {}
    for item in items:
        groups.setdefault(key_fn(item), []).append(item)

    results = []
    for group in groups.values():
        if len(group) < 2:
            continue
        kept, dupes = group[0], group[1:]
        provenance = tuple(dict.fromkeys(sum((list(i.provenance) or [i.item_id] for i in group), [])))
        results.append(DeduplicationResult(kept_item_id=kept.item_id, duplicate_item_ids=tuple(d.item_id for d in dupes), retained_provenance=provenance))
    return tuple(results)
