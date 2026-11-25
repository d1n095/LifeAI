"""Runtime health sweep and owner-scoped health summary.

This extends the existing runtime-incident table; it does not add a second observability
platform. The sweep is deliberately bounded and conservative:

* expired internal MainAI jobs are requeued at most up to their existing retry budget;
* expired `task_execution` jobs are never blindly requeued (they may have external effects);
* repeated findings are grouped through the incident fingerprint and a short repeat window;
* no prompt, reply, document text, credential or raw provider payload is written.

The worker calls `sweep_runtime_health()` on its normal poll cycle. The API calls
`health_summary()` with an owner id, so customer installations never see another owner's
incidents or jobs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.incidents.service import fingerprint_for, record_incident
from app.jobs.heartbeat import worker_process_alive
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.runtime_incident import RuntimeIncident

# A job sitting queued this long with no worker claim is worth surfacing even though it is
# not yet a crash. The worker's normal poll interval is seconds, not minutes.
STALE_QUEUE_SECONDS = 900
# Do not increment the same incident on every 2s poll. A recurring failure is still grouped;
# this only prevents a noisy occurrence count while the same condition persists.
INCIDENT_REPEAT_WINDOW_SECONDS = 300
# Bound every sweep. The worker has one poll loop and must not spend unbounded time here.
MAX_HEALTH_SCAN = 50
SAFE_RECLAIM_JOB_TYPES = frozenset(
    {"corpus_review", "message_sequence_backfill", "message_source_backfill"}
)


@dataclass
class HealthSweepResult:
    scanned: int = 0
    requeued: int = 0
    failed_exhausted: int = 0
    incidents: int = 0
    errors: list[str] = field(default_factory=list)


def _record_due(
    db: Session,
    *,
    owner_id: uuid.UUID,
    error_code: str,
    component: str,
    severity: str,
    summary: str,
    correlation_id: str | None,
    details: dict,
    now: datetime,
) -> bool:
    fingerprint = fingerprint_for(error_code=error_code, component=component, summary=summary)
    existing = db.execute(
        select(RuntimeIncident).where(
            RuntimeIncident.owner_id == owner_id,
            RuntimeIncident.fingerprint == fingerprint,
        )
    ).scalar_one_or_none()
    if (
        existing is not None
        and existing.status != "resolved"
        and existing.last_seen_at is not None
        and (now - existing.last_seen_at).total_seconds() < INCIDENT_REPEAT_WINDOW_SECONDS
    ):
        return False
    record_incident(
        db,
        owner_id=owner_id,
        error_code=error_code,
        component=component,
        severity=severity,
        summary=summary,
        correlation_id=correlation_id,
        app_version="0.1.0",
        details=details,
    )
    return True


def _scan_mainai_jobs(db: Session, *, now: datetime, result: HealthSweepResult) -> None:
    jobs = list(
        db.execute(
            select(MainAIJob)
            .where(
                MainAIJob.status == MainAIJobStatus.running,
                or_(
                    MainAIJob.lease_expires_at.is_(None),
                    MainAIJob.lease_expires_at < now,
                ),
            )
            .order_by(MainAIJob.lease_expires_at.asc().nullsfirst(), MainAIJob.created_at.asc())
            .limit(MAX_HEALTH_SCAN)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        result.scanned += 1
        if job.job_type == "task_execution":
            # A task_execution job can have committed/pushed external effects. It must go
            # through the existing inspect -> classify -> takeover pipeline, never a blind
            # requeue from this sweep.
            if _record_due(
                db,
                owner_id=job.owner_id,
                error_code="job_lease_expired_task_execution",
                component="worker.mainai_job",
                severity="high",
                summary="Task execution-lease gick ut; återhämtning krävs",
                correlation_id=str(job.id),
                details={
                    "job_id": str(job.id),
                    "failure_class": "lease_expired",
                    "external_effect_state": "unknown",
                    "occurrence_scope": "job",
                },
                now=now,
            ):
                result.incidents += 1
            continue

        if job.job_type not in SAFE_RECLAIM_JOB_TYPES:
            if _record_due(
                db,
                owner_id=job.owner_id,
                error_code="job_lease_expired_unrecoverable",
                component="worker.mainai_job",
                severity="high",
                summary="MainAI-jobbets lease gick ut utan säker återköning",
                correlation_id=str(job.id),
                details={
                    "job_id": str(job.id),
                    "failure_class": "lease_expired",
                    "occurrence_scope": "job",
                },
                now=now,
            ):
                result.incidents += 1
            continue

        if job.cancel_requested:
            if _record_due(
                db,
                owner_id=job.owner_id,
                error_code="job_cancelled_lease_expired",
                component="worker.mainai_job",
                severity="low",
                summary="Avbrutet jobb lämnade en utgången lease",
                correlation_id=str(job.id),
                details={"job_id": str(job.id), "failure_class": "cancelled"},
                now=now,
            ):
                result.incidents += 1
            continue

        retry_count = int(job.retry_count or 0)
        max_retries = int(job.max_retries or 0)
        if retry_count >= max_retries:
            job.status = MainAIJobStatus.failed
            job.error_category = "timeout"
            job.public_message = "Jobbet överskred återförsöksgränsen efter upprepade avbrott."
            job.completed_at = now
            job.locked_by = None
            job.lease_expires_at = None
            db.add(job)
            result.failed_exhausted += 1
            if _record_due(
                db,
                owner_id=job.owner_id,
                error_code="job_retry_exhausted",
                component="worker.mainai_job",
                severity="high",
                summary="MainAI-jobbets återförsöksgräns nåddes efter upprepade avbrott",
                correlation_id=str(job.id),
                details={
                    "job_id": str(job.id),
                    "failure_class": "retry_exhausted",
                    "occurrence_scope": "job",
                },
                now=now,
            ):
                result.incidents += 1
            continue

        # Safe, idempotent internal job types only. The claim SQL bumps lease_generation, so
        # a stale worker that wakes up later cannot write against this job.
        job.status = MainAIJobStatus.queued
        job.retry_count = retry_count + 1
        job.locked_by = None
        job.lease_expires_at = None
        job.completed_at = None
        job.error_category = None
        job.public_message = "Jobbet återköas efter en utgången lease."
        db.add(job)
        result.requeued += 1
        if _record_due(
            db,
            owner_id=job.owner_id,
            error_code="job_lease_expired",
            component="worker.mainai_job",
            severity="medium",
            summary="MainAI-jobbets lease gick ut och jobbet återköades",
            correlation_id=str(job.id),
            details={
                "job_id": str(job.id),
                "failure_class": "lease_expired",
                "external_effect_state": "none_proven",
                "occurrence_scope": "job",
            },
            now=now,
        ):
            result.incidents += 1

    stale_queued = list(
        db.execute(
            select(MainAIJob)
            .where(
                MainAIJob.status == MainAIJobStatus.queued,
                MainAIJob.created_at < now - timedelta(seconds=STALE_QUEUE_SECONDS),
            )
            .order_by(MainAIJob.created_at.asc())
            .limit(MAX_HEALTH_SCAN)
        )
        .scalars()
        .all()
    )
    for job in stale_queued:
        result.scanned += 1
        if _record_due(
            db,
            owner_id=job.owner_id,
            error_code="job_queue_stalled",
            component="worker.mainai_job",
            severity="medium",
            summary="MainAI-jobb väntar utan att någon worker tar det",
            correlation_id=str(job.id),
            details={"job_id": str(job.id), "failure_class": "queue_stalled"},
            now=now,
        ):
            result.incidents += 1


def _scan_import_jobs(db: Session, *, now: datetime, result: HealthSweepResult) -> None:
    jobs = list(
        db.execute(
            select(ImportJob)
            .where(
                ImportJob.status == ImportJobStatus.running,
                or_(
                    ImportJob.lease_expires_at.is_(None),
                    ImportJob.lease_expires_at < now,
                ),
            )
            .order_by(ImportJob.lease_expires_at.asc().nullsfirst(), ImportJob.created_at.asc())
            .limit(MAX_HEALTH_SCAN)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        result.scanned += 1
        if _record_due(
            db,
            owner_id=job.owner_id,
            error_code="import_lease_expired",
            component="worker.import_job",
            severity="medium",
            summary="Importjobbets lease gick ut och kan återtas av en worker",
            correlation_id=str(job.id),
            details={"job_id": str(job.id), "failure_class": "lease_expired"},
            now=now,
        ):
            result.incidents += 1


def sweep_runtime_health(
    db: Session, *, worker_id: str, now: datetime | None = None
) -> HealthSweepResult:
    """Detect stalled work and safely recover only bounded, idempotent internal jobs.

    Never raises: a health sweep failure must not stop the worker's poll loop. The caller
    owns the transaction and should commit after a successful sweep.
    """

    del worker_id  # reserved for structured logs; kept for a stable call contract
    now = now or datetime.utcnow()
    result = HealthSweepResult()
    try:
        _scan_mainai_jobs(db, now=now, result=result)
        _scan_import_jobs(db, now=now, result=result)
    except Exception as exc:  # noqa: BLE001 - health detection must degrade, never crash
        result.errors.append(type(exc).__name__)
        db.rollback()
    return result


def health_summary(db: Session, *, owner_id: uuid.UUID) -> dict:
    """Owner-scoped recovered/degraded/blocked view over the existing incident table."""

    now = datetime.utcnow()
    rows = list(
        db.execute(
            select(RuntimeIncident)
            .where(RuntimeIncident.owner_id == owner_id)
            .order_by(RuntimeIncident.last_seen_at.desc())
            .limit(500)
        )
        .scalars()
        .all()
    )
    open_rows = [row for row in rows if row.status != "resolved"]
    recent_resolved = [
        row
        for row in rows
        if row.status == "resolved" and row.resolved_at is not None and row.resolved_at >= now - timedelta(hours=24)
    ]

    severity_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    max_open = max((severity_order.get(row.severity, 0) for row in open_rows), default=0)

    stalled_jobs = int(
        db.execute(
            select(func.count())
            .select_from(MainAIJob)
            .where(
                MainAIJob.owner_id == owner_id,
                MainAIJob.status == MainAIJobStatus.running,
                or_(
                    MainAIJob.lease_expires_at.is_(None),
                    MainAIJob.lease_expires_at < now,
                ),
            )
        ).scalar_one()
    )
    stalled_imports = int(
        db.execute(
            select(func.count())
            .select_from(ImportJob)
            .where(
                ImportJob.owner_id == owner_id,
                ImportJob.status == ImportJobStatus.running,
                or_(
                    ImportJob.lease_expires_at.is_(None),
                    ImportJob.lease_expires_at < now,
                ),
            )
        ).scalar_one()
    )

    components: dict[str, dict] = {}
    for row in rows:
        entry = components.setdefault(
            row.component,
            {"component": row.component, "state": "healthy", "open": 0, "recovered": 0, "last_seen_at": row.last_seen_at},
        )
        if row.status == "resolved":
            if row.resolved_at is not None and row.resolved_at >= now - timedelta(hours=24):
                entry["recovered"] += 1
                entry["state"] = "recovered"
        else:
            entry["open"] += 1
            entry["state"] = "blocked" if row.severity == "critical" else "degraded"
        if row.last_seen_at and (entry["last_seen_at"] is None or row.last_seen_at > entry["last_seen_at"]):
            entry["last_seen_at"] = row.last_seen_at

    if max_open >= 4 or (stalled_jobs and max_open >= 3):
        state = "blocked"
    elif max_open >= 2 or stalled_jobs or stalled_imports:
        state = "degraded"
    elif recent_resolved:
        state = "recovered"
    else:
        state = "healthy"

    heartbeat = worker_process_alive()
    return {
        "state": state,
        "open_incidents": len(open_rows),
        "critical_open": sum(1 for row in open_rows if row.severity == "critical"),
        "high_open": sum(1 for row in open_rows if row.severity == "high"),
        "medium_open": sum(1 for row in open_rows if row.severity == "medium"),
        "low_open": sum(1 for row in open_rows if row.severity == "low"),
        "recovered_incidents": len(recent_resolved),
        "stalled_jobs": stalled_jobs,
        "stalled_imports": stalled_imports,
        "worker_heartbeat": "alive" if heartbeat is True else "unknown",
        "components": sorted(components.values(), key=lambda item: item["component"]),
        "checked_at": now,
    }
