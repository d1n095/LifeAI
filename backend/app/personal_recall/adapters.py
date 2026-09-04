from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.personal_recall.types import PersonalKnowledgeItem


class PersonalSourceAdapter(Protocol):
    """Storage formats stay behind this boundary; adapters must owner-scope reads."""

    name: str

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]: ...


class IterableAdapter:
    """Reference adapter used by tests, importers, and future store-specific adapters."""

    def __init__(self, name: str, items: Iterable[PersonalKnowledgeItem]):
        self.name = name
        self._items = list(items)

    def discover(self, *, owner_id: str) -> Iterable[PersonalKnowledgeItem]:
        return (item for item in self._items if item.owner_id == owner_id)


def collect_owner_items(adapters: Iterable[PersonalSourceAdapter], *, owner_id: str) -> tuple[list[PersonalKnowledgeItem], list[str]]:
    items: list[PersonalKnowledgeItem] = []
    warnings: list[str] = []
    for adapter in adapters:
        try:
            found = list(adapter.discover(owner_id=owner_id))
        except Exception as exc:  # adapter failures cannot make global recall look complete
            warnings.append(f"{adapter.name}: failed ({type(exc).__name__})")
            continue
        leaked = [item.item_id for item in found if item.owner_id != owner_id]
        if leaked:
            warnings.append(f"{adapter.name}: rejected {len(leaked)} cross-owner item(s)")
        items.extend(item for item in found if item.owner_id == owner_id)
    return items, warnings
