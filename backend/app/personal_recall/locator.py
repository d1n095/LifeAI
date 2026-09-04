from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.personal_recall.types import IndexState, PersonalKnowledgeItem

_LOCAL_SCHEMES = frozenset({"local", "memory", "conversation", "document", "file"})


class LocatorValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedLocator:
    scheme: str
    source_id: str
    locator: str


def validate_open_locator(item: PersonalKnowledgeItem, *, owner_id: str) -> ValidatedLocator:
    """Validate an inert locator. Validation never opens or executes the target."""
    if item.owner_id != owner_id:
        raise LocatorValidationError("locator belongs to another owner")
    if item.index_state in (IndexState.DELETED, IndexState.FAILED, IndexState.STALE):
        raise LocatorValidationError(f"source is {item.index_state.value}")
    parsed = urlsplit(item.provenance.locator)
    if parsed.scheme.casefold() not in _LOCAL_SCHEMES:
        raise LocatorValidationError("unsupported or non-local locator scheme")
    if parsed.username or parsed.password or parsed.port:
        raise LocatorValidationError("locator authority is not allowed")
    decoded = unquote(f"{parsed.netloc}{parsed.path}")
    segments = [part for part in decoded.replace("\\", "/").split("/") if part]
    if not segments or any(part in (".", "..") for part in segments) or "\x00" in decoded:
        raise LocatorValidationError("malformed or traversing locator")
    source_id = segments[-1]
    if source_id != item.source_id and source_id != item.item_id and source_id != item.content_reference.rsplit("/", 1)[-1]:
        raise LocatorValidationError("locator does not resolve to the declared source")
    return ValidatedLocator(parsed.scheme.casefold(), source_id, item.provenance.locator)
