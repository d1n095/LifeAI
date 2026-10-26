"""Reusable restriction handling inside the existing durable ledger."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO, DurableCoordinationService

CLASSIFICATIONS = (
    "technical",
    "contractual",
    "legal",
    "authorization",
    "financial",
    "safety",
    "unknown",
)
WORKAROUND_CLASSES = ("supported", "prohibited", "uncertain")
SECRET_KEY = re.compile(r"(token|secret|password|authorization|api[_-]?key|cookie|credential)", re.I)
BARE_CREDENTIAL_PREFIXES = ("sk-", "sk_live", "sk_test", "xoxb-", "ghp_", "key_live_")
_CREDENTIAL_BODY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
PROHIBITED_REASONS = frozenset({"bypass_restriction", "expand_authority", "violate_terms", "exceed_spend"})


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _redact_embedded_credentials(value: str) -> str:
    """Replace credential-shaped tokens even when they sit inside a sentence."""
    out = value
    for prefix in BARE_CREDENTIAL_PREFIXES:
        start = 0
        while True:
            pos = out.lower().find(prefix, start)
            if pos < 0:
                break
            end = pos + len(prefix)
            while end < len(out) and out[end] in _CREDENTIAL_BODY:
                end += 1
            if end == pos + len(prefix):
                start = end
                continue
            out = out[:pos] + "[redacted]" + out[end:]
            start = pos + len("[redacted]")
    return out


def redact_secrets(value: Any) -> Any:
    """Key-name redaction plus bare credential shapes. Reuses development-operator patterns."""
    from app.development_operator.service import redact as operator_redact

    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if any(stripped.startswith(prefix) for prefix in BARE_CREDENTIAL_PREFIXES):
        return "[redacted]"
    if stripped.lower().startswith("bearer "):
        return "[redacted]"
    operator = operator_redact(value)
    if operator != value:
        return operator.replace("[REDACTED]", "[redacted]")
    embedded = _redact_embedded_credentials(value)
    if embedded != value:
        return embedded
    if len(value) > 24 and SECRET_KEY.search(value):
        return "[redacted]"
    return value


def _fingerprint(*, classification: str, source: str, restriction: str) -> str:
    raw = f"{classification}|{source}|{restriction}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def encounter_restriction(
    service: DurableCoordinationService,
    *,
    owner_id: str,
    operation: str,
    restriction: str,
    source: str,
    affected_task: str | None,
    classification: str,
    confidence: float,
    alternatives: list[dict[str, Any]] | None = None,
    actor: str = "mainai",
    provider_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a material limitation and decide pause vs authorized troubleshooting."""
    kind = classification if classification in CLASSIFICATIONS else "unknown"
    options = []
    for row in alternatives or []:
        workaround = str(row.get("workaround_class") or "uncertain")
        if workaround not in WORKAROUND_CLASSES:
            workaround = "uncertain"
        options.append(
            {
                "description": str(row.get("description") or ""),
                "workaround_class": workaround,
                "reason": str(row.get("reason") or ""),
            }
        )
    prohibited = [row for row in options if row["workaround_class"] == "prohibited" or row.get("reason") in PROHIBITED_REASONS]
    uncertain = [row for row in options if row["workaround_class"] == "uncertain"]
    supported = [row for row in options if row["workaround_class"] == "supported"]
    pause = bool(prohibited or uncertain or not options)
    decision = "paused_pending_founder" if pause else "continue_authorized_troubleshooting"
    if provider_payload and (provider_payload.get("suppress") or provider_payload.get("independently_verified") or provider_payload.get("founder_authorized")):
        pause = True
        decision = "paused_provider_cannot_authorize"
    fingerprint = _fingerprint(classification=kind, source=source, restriction=restriction)
    record_id = str(uuid.uuid4())
    record = {
        "record_id": record_id,
        "version": 1,
        "owner_id": str(owner_id),
        "operation": operation,
        "restriction": restriction,
        "source": source,
        "affected_task": affected_task,
        "classification": kind,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "alternatives": options,
        "supported_alternatives": supported,
        "prohibited_workarounds": prohibited,
        "uncertain_workarounds": uncertain,
        "decision": decision,
        "status": "paused" if pause else "continuing",
        "actor": actor,
        "created_at": _iso(),
        "fingerprint": fingerprint,
        "evidence": redact_secrets(provider_payload or {}),
        "provider_cannot_suppress": True,
        "provider_cannot_rewrite_rules": True,
        "authorization_reused": False,
        "correctable": True,
        "search_text": f"{kind} {source} {restriction} {operation} {affected_task or ''}",
        "first_cycle_go": FIRST_CYCLE_GO,
    }
    service._restrictions.setdefault("records", []).append(record)
    service._restrictions.setdefault("by_fingerprint", {}).setdefault(fingerprint, []).append(record_id)
    return {
        "outcome": decision,
        "paused": pause,
        "record": record,
        "founder_visible": True,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def lookup_restriction_lessons(
    service: DurableCoordinationService,
    *,
    classification: str,
    source: str,
    restriction: str,
) -> dict[str, Any]:
    """Reuse verified lessons without assuming old authorization still holds."""
    fingerprint = _fingerprint(classification=classification, source=source, restriction=restriction)
    ids = service._restrictions.get("by_fingerprint", {}).get(fingerprint) or []
    records = [row for row in service._restrictions.get("records", []) if row.get("record_id") in ids]
    return {
        "fingerprint": fingerprint,
        "matches": records,
        "authorization_still_valid": False,
        "requires_reconfirmation": True,
        "count": len(records),
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def restriction_public_view(service: DurableCoordinationService) -> dict[str, Any]:
    records = list(service._restrictions.get("records") or [])
    return {
        "records": records,
        "open": [row for row in records if row.get("status") == "paused"],
        "continuing": [row for row in records if row.get("status") == "continuing"],
        "searchable": True,
        "versioned": True,
        "plaintext_secrets": False,
        "provider_cannot_suppress": True,
        "first_cycle_go": FIRST_CYCLE_GO,
        "dump_json": json.dumps([row["record_id"] for row in records]),
    }
