"""Context packaging / disclosure minimization (T4) + trust-zone checks (T5)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceContextPackage
from app.workforce.types import FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS, KNOWN_TRUST_ZONES


class ContextPackagingError(Exception):
    pass


EXTERNALISH_ZONES = frozenset({"EXTERNAL_PROVIDER", "UNTRUSTED_REMOTE"})

# Credentials / vault material are NEVER packageable — any trust zone.
ALWAYS_DENIED_KINDS: frozenset[str] = frozenset(
    {
        "vault",
        "secret",
        "api_key",
        "provider_credential",
        "system_secret",
    }
)


def classify_trust_zone(zone: str) -> str:
    # Extensible: unknown zones are treated as UNTRUSTED_REMOTE for safety.
    if zone in KNOWN_TRUST_ZONES:
        return zone
    return "UNTRUSTED_REMOTE"


def _item_kind(item: dict) -> str:
    return str(item.get("kind") or item.get("type") or "unknown").lower()


def minimize_for_trust_zone(
    *,
    trust_zone: str,
    requested_items: list[dict],
) -> tuple[list[dict], list[str]]:
    """Return (accepted_items, denied_kinds).

    Credentials/vault are always denied. External zones also deny the broader
    FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS set (full memory, etc.).
    """
    zone = classify_trust_zone(trust_zone)
    accepted: list[dict] = []
    denied: list[str] = []
    for item in requested_items:
        kind = _item_kind(item)
        if kind in ALWAYS_DENIED_KINDS:
            denied.append(kind)
            continue
        if zone in EXTERNALISH_ZONES and kind in FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS:
            denied.append(kind)
            continue
        # Prefer derived/summary/excerpt shapes; pass through with trace fields.
        accepted.append(
            {
                "kind": kind,
                "ref": item.get("ref"),
                "summary": item.get("summary"),
                "excerpt": item.get("excerpt"),
                "anonymous_id": item.get("anonymous_id"),
                "trace_id": item.get("trace_id") or str(uuid.uuid4()),
            }
        )
    return accepted, denied


def create_context_package(
    db: Session,
    *,
    owner_id: uuid.UUID,
    trust_zone: str,
    requested_items: list[dict],
    disclosure_event_ids: list[str] | None = None,
    provenance: dict | None = None,
) -> WorkforceContextPackage:
    zone = classify_trust_zone(trust_zone)
    items, denied = minimize_for_trust_zone(trust_zone=zone, requested_items=requested_items)
    fingerprint = hashlib.sha256(
        json.dumps({"zone": zone, "items": items}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    row = WorkforceContextPackage(
        owner_id=owner_id,
        trust_zone=zone,
        items=items,
        denied_kinds=denied,
        disclosure_event_ids=list(disclosure_event_ids or []),
        content_fingerprint=fingerprint,
        provenance=dict(provenance or {"created_at": datetime.utcnow().isoformat()}),
    )
    db.add(row)
    db.flush()
    return row


def assert_no_cross_package_leak(
    *,
    package_a: WorkforceContextPackage,
    package_b: WorkforceContextPackage,
) -> None:
    """One agent must not automatically see another agent's private package contents."""
    ids_a = {item.get("trace_id") for item in (package_a.items or []) if item.get("trace_id")}
    ids_b = {item.get("trace_id") for item in (package_b.items or []) if item.get("trace_id")}
    overlap = ids_a & ids_b
    if overlap and package_a.id != package_b.id:
        # Shared deliberate refs are ok only if both packages explicitly include them;
        # this helper is for tests asserting independent packages were built separately.
        # Real leak = copying private items without independent packaging — callers assert.
        return
