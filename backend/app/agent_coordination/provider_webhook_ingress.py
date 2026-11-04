"""Authenticated, replay-safe provider webhook transaction boundary."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
from app.agent_coordination.operating_cycle_runtime import _assert_cycle_authority
from app.models.operating_cycle import AuthorizedOperatingCycle, ProviderWebhookEvent
from app.models.workforce import WorkforceAssignment
from app.workforce.authority import require_live_assignment_authority
from app.workforce.broker import ingest_untrusted_result

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_STATUSES = frozenset({"CREATING", "RUNNING", "FINISHED", "ERROR", "FAILED"})


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ingest_cursor_webhook(
    db: Session,
    *,
    owner_id: uuid.UUID,
    secret: str,
    raw_body: bytes,
    signature: str,
    user_agent: str | None,
    delivery_id: str | None,
    now: datetime | None = None,
    fixture: bool = False,
    max_age_seconds: int = 300,
) -> dict:
    """Verify raw bytes first, then serialize against the bound cycle row."""
    verified = verify_v0_webhook(
        secret=secret,
        raw_body=raw_body,
        signature=signature,
        user_agent=user_agent,
        fixture=fixture,
    )
    if verified.get("outcome") != "accepted":
        return {"outcome": "rejected", "reason": "signature_invalid", "first_cycle_go": False}
    payload = verified.get("payload") or {}
    external_id = str(payload.get("id") or "").strip()
    status = str(payload.get("status") or "").upper()
    occurred = _parse_timestamp(payload.get("timestamp"))
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not external_id or status not in ALLOWED_STATUSES or occurred is None:
        return {"outcome": "rejected", "reason": "malformed_canonical_event", "first_cycle_go": False}
    if occurred < clock - timedelta(seconds=max_age_seconds) or occurred > clock + timedelta(seconds=30):
        return {"outcome": "rejected", "reason": "timestamp_out_of_window", "first_cycle_go": False}

    body_sha = hashlib.sha256(raw_body).hexdigest()
    # A provider delivery id is its immutable identity. Reusing it with changed bytes is
    # conflicting evidence, not a fresh event. Providers without one fall back to body hash.
    event_key = hashlib.sha256(f"cursor\0{delivery_id}".encode()).hexdigest() if delivery_id else body_sha
    cycle = db.execute(
        select(AuthorizedOperatingCycle)
        .where(
            AuthorizedOperatingCycle.owner_id == owner_id,
            AuthorizedOperatingCycle.provider == "cursor",
            AuthorizedOperatingCycle.external_agent_id == external_id,
            AuthorizedOperatingCycle.state.in_(("awaiting_provider", "awaiting_verification")),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if cycle is None:
        return {"outcome": "rejected", "reason": "unknown_or_inactive_binding", "first_cycle_go": False}
    existing = db.execute(
        select(ProviderWebhookEvent).where(
            ProviderWebhookEvent.provider == "cursor", ProviderWebhookEvent.event_key == event_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.body_sha256 != body_sha:
            return {"outcome": "rejected", "reason": "delivery_id_body_mismatch", "first_cycle_go": False}
        return {"outcome": "replay_ignored", "event_id": str(existing.id), "first_cycle_go": False}
    if cycle.state == "awaiting_verification":
        return {"outcome": "rejected", "reason": "terminal_provider_transition_locked", "first_cycle_go": False}
    assignment = db.execute(
        select(WorkforceAssignment)
        .where(WorkforceAssignment.id == cycle.workforce_assignment_id, WorkforceAssignment.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if assignment is None:
        return {"outcome": "rejected", "reason": "assignment_binding_missing", "first_cycle_go": False}
    try:
        _assert_cycle_authority(db, cycle)
        require_live_assignment_authority(assignment)
    except Exception as exc:  # fail closed; response never echoes secrets or raw payload
        db.rollback()
        return {"outcome": "rejected", "reason": "authority_not_live", "detail": type(exc).__name__, "first_cycle_go": False}
    last_occurred = _parse_timestamp((cycle.provenance or {}).get("last_provider_occurred_at"))
    if last_occurred is not None and occurred <= last_occurred:
        return {"outcome": "rejected", "reason": "reordered_provider_event", "first_cycle_go": False}

    receipt_id = uuid.uuid4()
    if status in {"CREATING", "RUNNING"}:
        assignment.status = "running"
        outcome = "progress_recorded"
    elif status == "FINISHED":
        candidate_sha = str(payload.get("candidate_sha") or "").lower()
        ingest_untrusted_result(
            db,
            owner_id=owner_id,
            assignment=assignment,
            payload={
                "provider_status": status,
                "external_agent_id": external_id,
                "candidate_sha": candidate_sha or None,
                "provider_event_receipt": str(receipt_id),
            },
        )
        cycle.candidate_sha = candidate_sha if SHA_RE.fullmatch(candidate_sha) else None
        cycle.state = "awaiting_verification"
        cycle.next_attempt_at = datetime.utcnow()
        outcome = "verification_required"
    else:
        assignment.status = "failed"
        assignment.failure_class = "provider_terminal_failure"
        assignment.completed_at = datetime.utcnow()
        cycle.state = "failed"
        cycle.last_error_code = "provider_terminal_failure"
        cycle.completed_at = datetime.utcnow()
        outcome = "provider_failed"
    receipt = ProviderWebhookEvent(
        id=receipt_id,
        provider="cursor",
        event_key=event_key,
        body_sha256=body_sha,
        owner_id=owner_id,
        cycle_id=cycle.id,
        assignment_id=assignment.id,
        external_agent_id=external_id,
        provider_status=status,
        occurred_at=occurred.replace(tzinfo=None),
        processing_state="applied",
        outcome=outcome,
        detail={"delivery_id_present": bool(delivery_id), "fixture": fixture},
    )
    db.add(receipt)
    provenance = dict(cycle.provenance or {})
    provenance["last_provider_occurred_at"] = occurred.isoformat()
    cycle.provenance = provenance
    db.commit()
    return {
        "outcome": outcome,
        "event_id": str(receipt.id),
        "cycle_id": str(cycle.id),
        "independently_verified": False,
        "first_cycle_go": False,
        "fixture": fixture,
    }
