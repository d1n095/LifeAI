"""Production Agent Messaging & Delivery Gateway.

Durable isolated inboxes on the existing DurableCoordinationService ledger. Creating an
inbox row is prepared, not delivered. A Founder-issued local authorized bridge that
successfully retrieves a payload is delivered. Recipient confirmation is acknowledged.
Vendor Cursor/Claude/Codex messaging stays BLOCKED. A message is never permission to
escalate authority, override STOP, write to GitHub, or mint independent certification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.durable import (
    FIRST_CYCLE_GO,
    DurableCoordinationService,
    ObligationStatus,
    load_owner_ledger,
    persist_to_assignment,
)
from app.agent_coordination.lifecycle import delivery_adapter_inventory
from app.audit import record_audit
from app.config import get_settings
from app.models.agent_coordination import AgentWorkAssignment

DELIVERY_STATES = (
    "prepared",
    "available",
    "claimed",
    "delivered",
    "acknowledged",
    "executing",
    "completed",
    "blocked",
    "failed",
)
ALLOWED_TRANSITIONS = {
    "prepared": {"available", "blocked", "failed"},
    "available": {"claimed", "delivered", "blocked", "failed"},
    "claimed": {"delivered", "blocked", "failed"},
    "delivered": {"acknowledged", "blocked", "failed"},
    "acknowledged": {"executing", "completed", "blocked", "failed"},
    "executing": {"completed", "blocked", "failed"},
    "completed": set(),
    "blocked": set(),
    "failed": set(),
}
KNOWN_RECIPIENTS = frozenset({"cursor", "claude", "codex", "examiner"})
FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "independently_verified",
        "independent_review_receipt",
        "first_cycle_go",
        "github_write",
        "override_stop",
        "stop",
        "personal_credentials",
    }
)
MISSING_VENDOR = "agent_messaging_adapter"
MISSING_LOCAL_BRIDGE = "local_authorized_bridge"
MAX_INSTRUCTION_CHARS = 8000
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 24 * 3600


def _iso(when: datetime | None = None) -> str:
    stamp = when or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def empty_delivery_state() -> dict[str, Any]:
    return {
        "messages": {},
        "bridges": {},
        "processed_ids": [],
        "decisions": [],
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
    }


def ensure_delivery(service: DurableCoordinationService) -> dict[str, Any]:
    state = getattr(service, "_delivery", None)
    if not isinstance(state, dict) or not state:
        service._delivery = empty_delivery_state()
    else:
        service._delivery.setdefault("messages", {})
        service._delivery.setdefault("bridges", {})
        service._delivery.setdefault("processed_ids", [])
        service._delivery.setdefault("decisions", [])
        service._delivery["first_cycle_go"] = FIRST_CYCLE_GO
        service._delivery["vendor_delivery_claimed"] = False
    return service._delivery


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_bridge_token(*, owner_id: str, recipient: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, dict[str, Any]]:
    if recipient not in KNOWN_RECIPIENTS:
        raise ValueError("unknown recipient")
    ttl = min(max(int(ttl_seconds), 60), MAX_TTL_SECONDS)
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    nonce = secrets.token_urlsafe(16)
    payload = f"{owner_id}:{recipient}:{int(expires.timestamp())}:{nonce}"
    secret = get_settings().secret_key.encode("utf-8")
    body = payload.encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    raw = secrets.token_urlsafe(24) + "." + payload + "." + sig
    record = {
        "recipient": recipient,
        "token_hash": _token_hash(raw),
        "issued_at": _iso(),
        "expires_at": _iso(expires),
        "revoked": False,
        "connected": False,
        "last_seen_at": None,
    }
    return raw, record


def parse_bridge_token(token: str) -> dict[str, Any] | None:
    if not token or token.count(".") < 2:
        return None
    try:
        _nonce, payload, sig = token.split(".", 2)
        secret = get_settings().secret_key.encode("utf-8")
        expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        owner_id, recipient, exp_raw, _nonce2 = payload.split(":", 3)
        if recipient not in KNOWN_RECIPIENTS:
            return None
        if int(exp_raw) < int(datetime.now(timezone.utc).timestamp()):
            return None
        uuid.UUID(owner_id)
        return {"owner_id": owner_id, "recipient": recipient, "token_hash": _token_hash(token)}
    except (ValueError, TypeError):
        return None


def transport_inventory(service: DurableCoordinationService | None = None) -> list[dict[str, Any]]:
    rows = delivery_adapter_inventory()
    live = []
    if service is not None:
        state = ensure_delivery(service)
        now = datetime.now(timezone.utc)
        for recipient, bridge in (state.get("bridges") or {}).items():
            expires = _parse_iso(bridge.get("expires_at"))
            if bridge.get("revoked") or expires is None or expires <= now:
                continue
            live.append(recipient)
    rows.insert(
        2,
        {
            "channel": "local_authorized_bridge",
            "kind": "messaging",
            "usable": bool(live),
            "delivery_claimed": False,
            "live_recipients": live,
            "missing_capability": None if live else MISSING_LOCAL_BRIDGE,
            "reason": "Founder-issued scoped inbox pull; not a vendor Cursor/Claude/Codex API",
        },
    )
    return rows


def _public_message(row: dict[str, Any], *, include_instructions: bool = False) -> dict[str, Any]:
    public = {
        "message_id": row.get("message_id"),
        "obligation_id": row.get("obligation_id"),
        "recipient": row.get("recipient"),
        "status": row.get("status"),
        "idempotency_key": row.get("idempotency_key"),
        "candidate_sha": row.get("candidate_sha"),
        "expires_at": row.get("expires_at"),
        "dependency_state": row.get("dependency_state") or {},
        "evidence": row.get("evidence") or {},
        "created_at": row.get("created_at"),
        "available_at": row.get("available_at"),
        "claimed_at": row.get("claimed_at"),
        "delivered_at": row.get("delivered_at"),
        "acknowledged_at": row.get("acknowledged_at"),
        "retries": row.get("retries") or 0,
        "missing_capability": row.get("missing_capability"),
        "adapter": row.get("adapter"),
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }
    if include_instructions:
        public["instructions"] = row.get("instructions")
    return public


def delivery_public_view(service: DurableCoordinationService) -> dict[str, Any]:
    state = ensure_delivery(service)
    messages = [_public_message(row) for row in (state.get("messages") or {}).values()]
    bridges = []
    for recipient, bridge in (state.get("bridges") or {}).items():
        expires = _parse_iso(bridge.get("expires_at"))
        live = not bridge.get("revoked") and expires is not None and expires > datetime.now(timezone.utc)
        bridges.append(
            {
                "recipient": recipient,
                "live": live,
                "connected": bool(bridge.get("connected")),
                "expires_at": bridge.get("expires_at"),
                "revoked": bool(bridge.get("revoked")),
                "token_present": False,
            }
        )
    return {
        "capability": "agent_messaging_delivery_gateway",
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
        "inbox_created_is_not_delivered": True,
        "messages": sorted(messages, key=lambda row: str(row.get("created_at") or "")),
        "bridges": bridges,
        "adapters": transport_inventory(service),
        "decisions": list(state.get("decisions") or [])[-20:],
        "blockers": [
            row
            for row in messages
            if row.get("status") in {"blocked", "failed"}
        ],
    }


def _record(service: DurableCoordinationService, *, kind: str, detail: dict[str, Any]) -> None:
    decisions = ensure_delivery(service).setdefault("decisions", [])
    decisions.append({"kind": kind, "at": _iso(), "detail": detail, "vendor_delivery_claimed": False})


def _transition(row: dict[str, Any], new_status: str) -> bool:
    current = row.get("status")
    if current == new_status:
        return True
    if new_status not in ALLOWED_TRANSITIONS.get(str(current), set()):
        return False
    row["status"] = new_status
    row["updated_at"] = _iso()
    return True


def persist_delivery(db: Session, *, owner_id: uuid.UUID, service: DurableCoordinationService) -> None:
    """Write delivery state even when the owner has no prior manual assignment (CF-1)."""
    from app.agent_coordination.persist import persist_owner_ledger

    persist_owner_ledger(db, owner_id=owner_id, service=service, actor="delivery")


def issue_bridge_credential(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipient: str,
    actor: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    if actor != "founder":
        return {"outcome": "forged_rejected", "reason": "only Founder may issue a bridge credential"}
    if recipient not in KNOWN_RECIPIENTS:
        return {"outcome": "rejected", "reason": "unknown recipient"}
    service = load_owner_ledger(db, owner_id=owner_id)
    state = ensure_delivery(service)
    token, record = mint_bridge_token(owner_id=str(owner_id), recipient=recipient, ttl_seconds=ttl_seconds)
    state["bridges"][recipient] = record
    persist_delivery(db, owner_id=owner_id, service=service)
    record_audit(
        db,
        user_id=owner_id,
        action="agent_delivery.bridge_issued",
        entity_type="agent_delivery_bridge",
        entity_id=recipient,
        detail="local_authorized_bridge",
        commit=False,
    )
    return {
        "outcome": "issued",
        "recipient": recipient,
        "token": token,
        "expires_at": record["expires_at"],
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def revoke_bridge_credential(db: Session, *, owner_id: uuid.UUID, recipient: str, actor: str) -> dict[str, Any]:
    if actor != "founder":
        return {"outcome": "forged_rejected", "reason": "only Founder may revoke a bridge credential"}
    service = load_owner_ledger(db, owner_id=owner_id)
    state = ensure_delivery(service)
    bridge = state["bridges"].get(recipient)
    if not bridge:
        return {"outcome": "stale_rejected", "reason": "no credential"}
    bridge["revoked"] = True
    persist_delivery(db, owner_id=owner_id, service=service)
    return {"outcome": "revoked", "recipient": recipient}


def resolve_bridge(db: Session, *, token: str) -> dict[str, Any] | None:
    parsed = parse_bridge_token(token)
    if parsed is None:
        return None
    owner_id = uuid.UUID(parsed["owner_id"])
    service = load_owner_ledger(db, owner_id=owner_id)
    state = ensure_delivery(service)
    stored = state["bridges"].get(parsed["recipient"])
    if stored is None or stored.get("revoked"):
        return None
    if stored.get("token_hash") != parsed["token_hash"]:
        return None
    expires = _parse_iso(stored.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return None
    return {
        "owner_id": owner_id,
        "recipient": parsed["recipient"],
        "service": service,
        "bridge": stored,
    }


def prepare_message(
    service: DurableCoordinationService,
    *,
    obligation_id: str,
    recipient: str,
    instructions: str,
    evidence: dict[str, Any] | None = None,
    dependency_state: dict[str, Any] | None = None,
    candidate_sha: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if recipient not in KNOWN_RECIPIENTS:
        return {"outcome": "rejected", "reason": "unknown recipient"}
    text = str(instructions or "").strip()
    if not text or len(text) > MAX_INSTRUCTION_CHARS:
        return {"outcome": "rejected", "reason": "invalid instructions"}
    key = idempotency_key or f"{obligation_id}:inbox"
    state = ensure_delivery(service)
    for row in state["messages"].values():
        if row.get("idempotency_key") == key:
            return {"outcome": "replay_ignored", "message": _public_message(row), "status": row.get("status")}
    expires = datetime.now(timezone.utc) + timedelta(seconds=min(max(ttl_seconds, 60), MAX_TTL_SECONDS))
    message_id = str(uuid.uuid4())
    row = {
        "message_id": message_id,
        "obligation_id": obligation_id,
        "recipient": recipient,
        "instructions": text,
        "evidence": dict(evidence or {}),
        "dependency_state": dict(dependency_state or {}),
        "candidate_sha": candidate_sha,
        "expires_at": _iso(expires),
        "idempotency_key": key,
        "status": "prepared",
        "created_at": _iso(),
        "retries": 0,
        "adapter": None,
        "missing_capability": None,
    }
    state["messages"][message_id] = row
    _record(service, kind="inbox_prepared", detail={"message_id": message_id, "recipient": recipient})
    return {"outcome": "prepared", "message": _public_message(row), "status": "prepared"}


def _live_bridge(state: dict[str, Any], recipient: str) -> dict[str, Any] | None:
    bridge = (state.get("bridges") or {}).get(recipient)
    if not bridge or bridge.get("revoked"):
        return None
    expires = _parse_iso(bridge.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return None
    return bridge


def attempt_transmit(service: DurableCoordinationService, *, message_id: str) -> dict[str, Any]:
    state = ensure_delivery(service)
    row = state["messages"].get(message_id)
    if row is None:
        return {"outcome": "stale_rejected", "reason": "unknown message"}
    expires = _parse_iso(row.get("expires_at"))
    if expires is not None and expires <= datetime.now(timezone.utc):
        _transition(row, "failed")
        row["missing_capability"] = "message_expired"
        return {"outcome": "failed", "status": "failed", "reason": "expired"}
    if row.get("status") not in {"prepared", "available"}:
        return {"outcome": "replay_ignored", "status": row.get("status"), "message": _public_message(row)}
    if _live_bridge(state, row["recipient"]) is None:
        _transition(row, "blocked")
        row["adapter"] = "local_authorized_bridge"
        row["missing_capability"] = MISSING_LOCAL_BRIDGE
        _record(service, kind="transmit_blocked", detail={"message_id": message_id, "missing": MISSING_LOCAL_BRIDGE})
        return {
            "outcome": "blocked",
            "status": "blocked",
            "missing_capability": MISSING_LOCAL_BRIDGE,
            "vendor_delivery_claimed": False,
            "message": _public_message(row),
        }
    _transition(row, "available")
    row["adapter"] = "local_authorized_bridge"
    row["available_at"] = _iso()
    row["missing_capability"] = None
    _record(service, kind="transmit_available", detail={"message_id": message_id, "adapter": "local_authorized_bridge"})
    return {"outcome": "available", "status": "available", "message": _public_message(row), "vendor_delivery_claimed": False}


def list_inbox(service: DurableCoordinationService, *, recipient: str) -> list[dict[str, Any]]:
    state = ensure_delivery(service)
    rows = []
    for row in state["messages"].values():
        if row.get("recipient") != recipient:
            continue
        if row.get("status") not in {"available", "claimed"}:
            continue
        rows.append(_public_message(row, include_instructions=False))
    return rows


def claim_message(service: DurableCoordinationService, *, recipient: str, message_id: str) -> dict[str, Any]:
    state = ensure_delivery(service)
    row = state["messages"].get(message_id)
    if row is None or row.get("recipient") != recipient:
        return {"outcome": "forged_rejected", "reason": "not this recipient's message"}
    expires = _parse_iso(row.get("expires_at"))
    if expires is not None and expires <= datetime.now(timezone.utc):
        _transition(row, "failed")
        row["missing_capability"] = "message_expired"
        return {"outcome": "failed", "reason": "expired", "status": "failed"}
    if row.get("status") == "delivered":
        return {"outcome": "replay_ignored", "status": "delivered", "message": _public_message(row, include_instructions=True)}
    if row.get("status") not in {"available", "claimed"}:
        return {"outcome": "stale_rejected", "reason": f"not claimable from {row.get('status')}", "status": row.get("status")}
    _transition(row, "claimed")
    row["claimed_at"] = row.get("claimed_at") or _iso()
    _transition(row, "delivered")
    row["delivered_at"] = _iso()
    bridge = state["bridges"].get(recipient)
    if bridge:
        bridge["connected"] = True
        bridge["last_seen_at"] = _iso()
    _record(service, kind="delivered", detail={"message_id": message_id, "recipient": recipient})
    return {
        "outcome": "delivered",
        "status": "delivered",
        "message": _public_message(row, include_instructions=True),
        "vendor_delivery_claimed": False,
    }


def submit_receipt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipient: str,
    payload: dict[str, Any],
    service: DurableCoordinationService | None = None,
) -> dict[str, Any]:
    if any(payload.get(key) for key in FORBIDDEN_RECEIPT_KEYS):
        return {"outcome": "forged_rejected", "reason": "privilege escalation or fabricated certification rejected"}
    kind = str(payload.get("kind") or "")
    if kind not in {"acknowledge", "execution_progress", "completion", "blocked", "failed"}:
        return {"outcome": "rejected", "reason": "unknown receipt kind"}
    event_id = str(payload.get("event_id") or "")
    if not event_id or len(event_id) > 128:
        return {"outcome": "rejected", "reason": "event_id required"}
    claimed_identity = str(payload.get("agent_identity") or recipient)
    if claimed_identity != recipient:
        return {"outcome": "forged_rejected", "reason": "spoofed agent identity"}
    service = service or load_owner_ledger(db, owner_id=owner_id)
    state = ensure_delivery(service)
    if event_id in state["processed_ids"]:
        return {"outcome": "replay_ignored", "reason": "duplicate event_id"}
    message_id = str(payload.get("message_id") or "")
    row = state["messages"].get(message_id)
    if row is None or row.get("recipient") != recipient:
        return {"outcome": "forged_rejected", "reason": "unknown or foreign message"}
    if kind == "acknowledge":
        if row.get("status") != "delivered":
            return {"outcome": "stale_rejected", "reason": "acknowledge requires delivered"}
        _transition(row, "acknowledged")
        row["acknowledged_at"] = _iso()
        obligation = service.get(row["obligation_id"])
        if obligation and obligation.get("status") == ObligationStatus.assigned.value:
            service.acknowledge(
                assignment_id=row["obligation_id"],
                agent_identity=recipient,
                event_id=f"ack:{event_id}",
            )
    elif kind == "execution_progress":
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            return {"outcome": "rejected", "reason": "execution requires evidence"}
        if row.get("status") not in {"acknowledged", "executing"}:
            return {"outcome": "stale_rejected", "reason": "execution requires acknowledged"}
        _transition(row, "executing")
        row.setdefault("evidence", {}).update(evidence)
        service.record_execution_progress(
            assignment_id=row["obligation_id"],
            agent_identity=recipient,
            evidence_kind="adapter_heartbeat",
            event_id=f"exec:{event_id}",
            evidence=evidence,
        )
    elif kind == "completion":
        if row.get("status") not in {"acknowledged", "executing", "delivered"}:
            return {"outcome": "stale_rejected", "reason": "completion requires received work"}
        _transition(row, "completed")
        service.complete(
            assignment_id=row["obligation_id"],
            agent_identity=recipient,
            event_id=f"complete:{event_id}",
            evidence=payload.get("evidence") or {"source": "local_authorized_bridge", "kind": "adapter_heartbeat"},
        )
    elif kind in {"blocked", "failed"}:
        _transition(row, kind)
        row["missing_capability"] = payload.get("missing_capability") or row.get("missing_capability")
    state["processed_ids"].append(event_id)
    persist_delivery(db, owner_id=owner_id, service=service)
    record_audit(
        db,
        user_id=owner_id,
        action=f"agent_delivery.receipt.{kind}",
        entity_type="agent_delivery_message",
        entity_id=message_id,
        detail=recipient,
        commit=False,
    )
    return {
        "outcome": "applied",
        "status": row.get("status"),
        "message": _public_message(row),
        "independently_verified": False,
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def reconcile_owner_delivery(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    service = load_owner_ledger(db, owner_id=owner_id)
    ensure_delivery(service)
    prepared = 0
    for assignment_id, record in list(service._obligations.items()):
        recipient = record.get("agent_identity")
        if recipient not in KNOWN_RECIPIENTS:
            continue
        if record.get("status") in {
            ObligationStatus.superseded.value,
            ObligationStatus.failed.value,
            ObligationStatus.independently_verified.value,
        }:
            continue
        instructions = (
            f"Obligation {assignment_id} for {recipient}: {record.get('workstream') or 'work'}. "
            f"Status={record.get('status')}. GitHub-green is not independent certification. "
            f"Do not write to GitHub. Do not override STOP."
        )
        result = prepare_message(
            service,
            obligation_id=assignment_id,
            recipient=recipient,
            instructions=instructions,
            evidence={"status": record.get("status")},
            dependency_state={"dependencies": record.get("dependencies") or []},
            candidate_sha=(record.get("detail") or {}).get("head_sha") if isinstance(record.get("detail"), dict) else None,
            idempotency_key=f"{assignment_id}:inbox",
        )
        if result.get("outcome") == "prepared":
            prepared += 1
    transmitted = 0
    for message_id, row in list(ensure_delivery(service)["messages"].items()):
        if row.get("status") == "prepared":
            attempt_transmit(service, message_id=message_id)
            transmitted += 1
        elif row.get("status") in {"available", "claimed"}:
            expires = _parse_iso(row.get("expires_at"))
            if expires is not None and expires <= datetime.now(timezone.utc):
                _transition(row, "failed")
                row["missing_capability"] = "message_expired"
    persist_delivery(db, owner_id=owner_id, service=service)
    return {"prepared": prepared, "transmitted": transmitted, "first_cycle_go": FIRST_CYCLE_GO}


def reconcile_pending_deliveries(db: Session, *, limit: int = 20) -> int:
    owner_ids = list(db.execute(select(AgentWorkAssignment.owner_id).distinct().limit(limit)).scalars())
    for owner_id in owner_ids:
        reconcile_owner_delivery(db, owner_id=owner_id)
    return len(owner_ids)
