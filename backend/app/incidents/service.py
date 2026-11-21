"""Runtime incident recording, grouping and bounded analysis.

The service is deliberately small and deterministic. It reuses the existing failure paths'
already-safe classifications; it never reads prompts, replies, documents or secrets. There is
no automatic remediation and no network upload: recovery options are suggestions for the
owner, and central diagnostics remain an explicit future opt-in.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.runtime_incident import RuntimeIncident

logger = logging.getLogger(__name__)

SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "acknowledged", "resolved")

# Stable, safe labels. A caller must never pass a raw provider response, prompt, reply or
# document fragment as `summary`; this service additionally strips unknown detail keys.
_SAFE_DETAIL_KEYS = frozenset(
    {
        "provider",
        "model",
        "category",
        "error_category",
        "job_id",
        "task_id",
        "assignment_id",
        "worker_id",
        "attempt_id",
        "duration_ms",
        "http_status",
        "failure_class",
        "external_effect_state",
        "streaming",
        "occurrence_scope",
    }
)
_FORBIDDEN_KEY_PARTS = (
    "prompt",
    "content",
    "message",
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
    "cookie",
    "url",
    "body",
    "query",
)


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:256]
    return None


def sanitize_details(details: dict | None) -> dict:
    """Keep only allowlisted, bounded scalar fields.

    Unknown keys are dropped, not merely truncated: a future caller cannot accidentally put
    a conversation or credential into the incident table by adding a new detail name.
    """

    clean: dict = {}
    for key, value in (details or {}).items():
        if not isinstance(key, str) or key not in _SAFE_DETAIL_KEYS:
            continue
        if any(part in key.lower() for part in _FORBIDDEN_KEY_PARTS):
            continue
        scalar = _safe_scalar(value)
        if scalar is not None:
            clean[key] = scalar
    return clean


def fingerprint_for(*, error_code: str, component: str, summary: str) -> str:
    material = f"{error_code.strip()}|{component.strip()}|{summary.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def analyze_incident(
    *, error_code: str, component: str, severity: str, details: dict | None = None
) -> tuple[dict, list[dict]]:
    """Return (analysis, safe recovery options) for a known failure class.

    The analysis is an explanation, never a claim that the cause is proven. Automatic
    remediation is always ``none`` in this first version.
    """

    code = (error_code or "").lower()
    comp = (component or "").lower()
    if "timeout" in code or "rate" in code or "unreachable" in code:
        analysis = {
            "known_cause": "provider_transport_or_capacity",
            "confidence": "likely",
            "reason": "Leverantören svarade inte inom tidsgränsen eller begränsade anropet.",
            "automatic_action": "none",
        }
        options = [
            {
                "action": "retry_after_provider_recovers",
                "safe": True,
                "requires_owner": True,
                "description": "Försök igen först när leverantören är nåbar; kontrollera kvot och konfiguration.",
            },
            {
                "action": "inspect_provider_status",
                "safe": True,
                "requires_owner": True,
                "description": "Kontrollera providerstatus och den valda modellen i Admin → Providers.",
            },
        ]
    elif "worker" in code or "worker" in comp or "crash" in code:
        analysis = {
            "known_cause": "worker_process_or_lease",
            "confidence": "likely",
            "reason": "En worker-process eller dess lease avslutades onormalt.",
            "automatic_action": "none",
        }
        options = [
            {
                "action": "inspect_worker_heartbeat",
                "safe": True,
                "requires_owner": True,
                "description": "Kontrollera worker-heartbeat och jobbets lease innan jobbet startas om.",
            },
            {
                "action": "resume_after_lease_review",
                "safe": True,
                "requires_owner": True,
                "description": "Återuppta först när lease och extern effekt är verifierade; kör inte om blint.",
            },
        ]
    elif "sandbox" in code or "sandbox" in comp:
        analysis = {
            "known_cause": "sandbox_startup",
            "confidence": "likely",
            "reason": "Sandbox-/adapterprocessen kunde inte starta.",
            "automatic_action": "none",
        }
        options = [
            {
                "action": "inspect_adapter_configuration",
                "safe": True,
                "requires_owner": True,
                "description": "Kontrollera adapterkommando, arbetskatalog och behörigheter.",
            }
        ]
    else:
        analysis = {
            "known_cause": "unknown",
            "confidence": "unknown",
            "reason": "Ingen säker känd orsak kan fastställas från den registrerade felkoden.",
            "automatic_action": "none",
        }
        options = [
            {
                "action": "inspect_local_history",
                "safe": True,
                "requires_owner": True,
                "description": "Granska incidentens tid, komponent och upprepningar lokalt.",
            }
        ]
    if severity in ("high", "critical"):
        options.append(
            {
                "action": "escalate_to_owner",
                "safe": True,
                "requires_owner": True,
                "description": "Låt behörig ägare granska innan någon automatisk åtgärd övervägs.",
            }
        )
    return analysis, options


def record_incident(
    db: Session,
    *,
    owner_id: uuid.UUID,
    error_code: str,
    component: str,
    severity: str,
    summary: str,
    correlation_id: str | None = None,
    app_version: str | None = None,
    details: dict | None = None,
) -> RuntimeIncident:
    """Insert or group one incident. The caller owns the transaction commit."""

    if severity not in SEVERITIES:
        raise ValueError(f"unknown incident severity: {severity}")
    if not error_code.strip() or not component.strip() or not summary.strip():
        raise ValueError("incident error_code, component and summary are required")
    safe_details = sanitize_details(details)
    fingerprint = fingerprint_for(
        error_code=error_code, component=component, summary=summary
    )
    now = datetime.utcnow()
    analysis, recovery_options = analyze_incident(
        error_code=error_code,
        component=component,
        severity=severity,
        details=safe_details,
    )

    def _update(row: RuntimeIncident) -> RuntimeIncident:
        row.occurrence_count = int(row.occurrence_count or 0) + 1
        row.last_seen_at = now
        row.severity = _max_severity(row.severity, severity)
        row.correlation_id = correlation_id or row.correlation_id
        row.app_version = app_version or row.app_version
        row.details = {**(row.details or {}), **safe_details}
        row.analysis = analysis
        row.recovery_options = recovery_options
        row.updated_at = now
        if row.status == "resolved":
            row.status = "open"
            row.resolved_at = None
        return row

    existing = (
        db.execute(
            select(RuntimeIncident)
            .where(
                RuntimeIncident.owner_id == owner_id,
                RuntimeIncident.fingerprint == fingerprint,
            )
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if existing is not None:
        return _update(existing)

    row = RuntimeIncident(
        owner_id=owner_id,
        fingerprint=fingerprint,
        error_code=error_code,
        component=component,
        severity=severity,
        status="open",
        summary=summary[:512],
        correlation_id=correlation_id,
        app_version=app_version,
        occurrence_count=1,
        first_seen_at=now,
        last_seen_at=now,
        details=safe_details,
        analysis=analysis,
        recovery_options=recovery_options,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # A concurrent recorder won the unique (owner, fingerprint) race. Group into the
        # winner instead of creating a second row.
        winner = (
            db.execute(
                select(RuntimeIncident)
                .where(
                    RuntimeIncident.owner_id == owner_id,
                    RuntimeIncident.fingerprint == fingerprint,
                )
                .with_for_update()
            )
            .scalar_one()
        )
        return _update(winner)


def record_incident_standalone(**kwargs) -> RuntimeIncident | None:
    """Persist one incident in its own transaction.

    Failure paths frequently run inside a caller-owned transaction that will be rolled back
    when the original error is re-raised. An incident recorded there would disappear with it.
    This helper opens a separate owner-scoped session, sets the same RLS identity the request
    path uses, commits only the incident, and never raises into the original failure path.
    """

    from app.db import SessionLocal
    from app.request_context import current_user_id

    owner_id = kwargs["owner_id"]
    token = current_user_id.set(str(owner_id))
    db = SessionLocal()
    try:
        # The session's after_begin hook re-applies this contextvar on every transaction
        # (including the savepoint inside record_incident). Setting only the GUC here would
        # be overwritten by a prior request's context on that savepoint.
        db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(owner_id)},
        )
        row = record_incident(db, **kwargs)
        db.commit()
        return row
    except Exception:
        db.rollback()
        logger.exception("could not persist runtime incident independently")
        return None
    finally:
        db.close()
        current_user_id.reset(token)


def _max_severity(left: str, right: str) -> str:
    order = {value: index for index, value in enumerate(SEVERITIES)}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def list_incidents(
    db: Session,
    *,
    owner_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
) -> list[RuntimeIncident]:
    stmt = select(RuntimeIncident).where(RuntimeIncident.owner_id == owner_id)
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown incident status: {status}")
        stmt = stmt.where(RuntimeIncident.status == status)
    return list(
        db.execute(
            stmt.order_by(RuntimeIncident.last_seen_at.desc()).limit(max(1, min(limit, 200)))
        ).scalars()
    )


def get_incident(
    db: Session, *, owner_id: uuid.UUID, incident_id: uuid.UUID
) -> RuntimeIncident | None:
    return db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.id == incident_id,
            RuntimeIncident.owner_id == owner_id,
        )
    ).scalar_one_or_none()


def acknowledge_incident(
    db: Session, *, owner_id: uuid.UUID, incident_id: uuid.UUID
) -> RuntimeIncident | None:
    row = get_incident(db, owner_id=owner_id, incident_id=incident_id)
    if row is None:
        return None
    if row.status == "open":
        row.status = "acknowledged"
        row.acknowledged_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
    return row


def resolve_incident(
    db: Session, *, owner_id: uuid.UUID, incident_id: uuid.UUID
) -> RuntimeIncident | None:
    row = get_incident(db, owner_id=owner_id, incident_id=incident_id)
    if row is None:
        return None
    row.status = "resolved"
    row.resolved_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    return row
