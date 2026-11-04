from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.personal_recall.types import PersonalKnowledgeItem, SourceType


class PersonalSourceAdapter(Protocol):
    """Storage formats stay behind this boundary; adapters must owner-scope reads."""

    name: str

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]: ...


class IterableAdapter:
    """Reference adapter used by tests, importers, and future store-specific adapters."""

    def __init__(self, name: str, items: Iterable[PersonalKnowledgeItem], *, source_types: Iterable[SourceType] | None = None):
        self.name = name
        self._items = list(items)
        self.source_types = frozenset(source_types or (item.source_type for item in self._items))

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        return (item for item in self._items if item.owner_id == owner_id)


def collect_owner_items(
    adapters: Iterable[PersonalSourceAdapter], *, owner_id: str, max_items: int
) -> tuple[list[PersonalKnowledgeItem], list[str], set[SourceType], list[str], bool]:
    items: list[PersonalKnowledgeItem] = []
    warnings: list[str] = []
    searched_types: set[SourceType] = set()
    failed_adapters: list[str] = []
    truncated = False
    for adapter in adapters:
        searched_types.update(getattr(adapter, "source_types", ()))
        try:
            found = list(adapter.discover(owner_id=owner_id))
        except Exception as exc:  # adapter failures cannot make global recall look complete
            warnings.append(f"{adapter.name}: failed ({type(exc).__name__})")
            failed_adapters.append(adapter.name)
            continue
        leaked = [item.item_id for item in found if item.owner_id != owner_id]
        if leaked:
            warnings.append(f"{adapter.name}: rejected {len(leaked)} cross-owner item(s)")
        for item in found:
            if item.owner_id != owner_id:
                continue
            if len(items) >= max_items:
                truncated = True
                break
            items.append(item)
        if truncated:
            warnings.append(f"retrieval bounded at {max_items} items")
            break
    return items, warnings, searched_types, failed_adapters, truncated
