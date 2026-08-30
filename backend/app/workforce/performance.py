"""Agent performance & trust ledger (T6). CONFIDENCE != PERFORMANCE EVIDENCE."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforcePerformanceRollup


class PerformanceLedgerError(Exception):
    pass


def get_or_create_rollup(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    capability_tag: str,
) -> WorkforcePerformanceRollup:
    row = db.execute(
        select(WorkforcePerformanceRollup).where(
            WorkforcePerformanceRollup.owner_id == owner_id,
            WorkforcePerformanceRollup.profile_id == profile_id,
            WorkforcePerformanceRollup.capability_tag == capability_tag,
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = WorkforcePerformanceRollup(
        owner_id=owner_id,
        profile_id=profile_id,
        capability_tag=capability_tag,
    )
    db.add(row)
    db.flush()
    return row


def record_job_attempt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    capability_tag: str,
) -> WorkforcePerformanceRollup:
    row = get_or_create_rollup(db, owner_id=owner_id, profile_id=profile_id, capability_tag=capability_tag)
    row.jobs_attempted = int(row.jobs_attempted) + 1
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def record_verified_outcome(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    capability_tag: str,
    success: bool,
    latency_ms: int = 0,
    provider_cost_usd: float = 0.0,
    token_usage: int = 0,
    tool_cost_usd: float = 0.0,
    quality_score: float | None = None,
    hallucination_or_factual_error: bool = False,
    security_violation: bool = False,
    authority_violation: bool = False,
    recovery_failure: bool = False,
    founder_correction: bool = False,
    reviewer_correction: bool = False,
    # Deliberately NOT accepting a "self_confidence" or "agent_trust_claim" parameter —
    # CONFIDENCE != PERFORMANCE EVIDENCE; agents cannot rate themselves into trust.
) -> WorkforcePerformanceRollup:
    row = get_or_create_rollup(db, owner_id=owner_id, profile_id=profile_id, capability_tag=capability_tag)
    row.jobs_completed = int(row.jobs_completed) + 1
    if success:
        row.verified_success = int(row.verified_success) + 1
    else:
        row.verified_failure = int(row.verified_failure) + 1
    row.latency_ms_sum = int(row.latency_ms_sum) + max(0, latency_ms)
    row.provider_cost_usd_sum = float(row.provider_cost_usd_sum) + max(0.0, provider_cost_usd)
    row.token_usage_sum = int(row.token_usage_sum) + max(0, token_usage)
    row.tool_cost_usd_sum = float(row.tool_cost_usd_sum) + max(0.0, tool_cost_usd)
    if hallucination_or_factual_error:
        row.hallucination_or_factual_errors = int(row.hallucination_or_factual_errors) + 1
    if security_violation:
        row.security_violations = int(row.security_violations) + 1
    if authority_violation:
        row.authority_violations = int(row.authority_violations) + 1
    if recovery_failure:
        row.recovery_failures = int(row.recovery_failures) + 1
    if founder_correction:
        row.founder_corrections = int(row.founder_corrections) + 1
    if reviewer_correction:
        row.reviewer_corrections = int(row.reviewer_corrections) + 1
    if quality_score is not None:
        row.quality_score_sum = float(row.quality_score_sum) + float(quality_score)
        row.quality_score_count = int(row.quality_score_count) + 1
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def verified_success_rate(row: WorkforcePerformanceRollup) -> float | None:
    total = int(row.verified_success) + int(row.verified_failure)
    if total == 0:
        return None
    return float(row.verified_success) / float(total)
