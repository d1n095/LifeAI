"""Explicit adapter registry. Empty in production until a real format is verified."""

from dataclasses import dataclass

from app.structured_import.adapter import StructuredExportAdapter, StructuredItemProcessor


class UnknownStructuredExportAdapter(LookupError):
    pass


@dataclass(frozen=True)
class AdapterBinding:
    adapter: StructuredExportAdapter
    processor: StructuredItemProcessor


_bindings: dict[str, AdapterBinding] = {}


def register_adapter(binding: AdapterBinding) -> None:
    key = binding.adapter.key
    if not key or len(key) > 128:
        raise ValueError("adapter key must contain 1..128 characters")
    if key in _bindings:
        raise ValueError(f"adapter {key!r} is already registered")
    _bindings[key] = binding


def unregister_adapter(key: str) -> None:
    _bindings.pop(key, None)


def resolve_adapter(key: str) -> AdapterBinding:
    try:
        return _bindings[key]
    except KeyError as exc:
        raise UnknownStructuredExportAdapter(key) from exc
