"""Live discovery of models installed in the local Ollama runtime.

The shared model catalog (app/providers/catalog.py) is auditable metadata, not a statement
about what is actually pulled on this host. This module answers the narrower operational
question the founder needs before selecting a local model: which exact tags does
``GET /api/tags`` report right now?

It is discovery only. It does not configure a provider, verify a model, authorize spend,
or invoke inference. The existing admin selection path still requires an explicit
verification before a model can be activated.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class LocalDiscoveryError(RuntimeError):
    """The local runtime answered, but not with a usable model list."""


@dataclass(frozen=True)
class LocalModel:
    name: str
    size_bytes: int | None = None
    modified_at: str | None = None
    digest: str | None = None


def _bounded_text(value: object, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or any(ord(char) < 32 for char in value):
        return None
    return value


def parse_local_models(payload: object) -> list[LocalModel]:
    """Parse Ollama's untrusted ``/api/tags`` body into a bounded, deduplicated list."""

    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise LocalDiscoveryError("Lokal modellruntime gav ett svar utan modellista.")

    models: list[LocalModel] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _bounded_text(entry.get("name")) or _bounded_text(entry.get("model"))
        if name is None or name in seen:
            continue
        seen.add(name)
        raw_size = entry.get("size")
        size_bytes = raw_size if type(raw_size) is int and raw_size >= 0 else None
        models.append(
            LocalModel(
                name=name,
                size_bytes=size_bytes,
                modified_at=_bounded_text(entry.get("modified_at"), limit=64),
                digest=_bounded_text(entry.get("digest"), limit=128),
            )
        )
        if len(models) >= 200:
            break
    return sorted(models, key=lambda model: model.name)


async def discover_local_models(
    base_url: str,
    *,
    timeout: float = 2.0,
) -> list[LocalModel]:
    """Read installed tags without following redirects or inheriting proxy settings."""

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LocalDiscoveryError("Lokal modellruntime kunde inte läsas.") from exc
    return parse_local_models(payload)
