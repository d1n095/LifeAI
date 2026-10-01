"""Governed ingestion and fail-closed provider error translation."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.types import (
    AdapterResult,
    EvidenceContext,
    FactMutability,
    ObservationStatus,
    ProviderObservation,
)
from app.claim_action_integrity.service import ClaimActionIntegrityError, record_evidence


class AdapterUnavailable(RuntimeError):
    pass


class AdapterAuthenticationError(AdapterUnavailable):
    pass


class AdapterRateLimited(AdapterUnavailable):
    pass


class MalformedProviderResponse(ValueError):
    pass


def ingest_observation(db: Session, context: EvidenceContext, observation: ProviderObservation) -> AdapterResult:
    """Validate an observation envelope, then use the kernel's only evidence write path."""
    required = {"owner_id", "execution_id", "subject", "task_id"}
    expected = {
        "owner_id": str(context.owner_id),
        "execution_id": context.execution_id,
        "subject": context.subject_key,
        "task_id": context.task_id or "",
    }
    if not observation.provider_response_id.strip():
        return AdapterResult(ObservationStatus.invalid_evidence, reason="provider_response_identity_missing")
    if not required.issubset(observation.bindings):
        return AdapterResult(ObservationStatus.invalid_evidence, reason="common_bindings_missing")
    if any(observation.bindings[key] != value for key, value in expected.items()):
        return AdapterResult(ObservationStatus.invalid_evidence, reason="context_binding_mismatch")
    if observation.fact_mutability is FactMutability.mutable_snapshot:
        if not observation.max_age_seconds or observation.max_age_seconds <= 0:
            return AdapterResult(ObservationStatus.invalid_evidence, reason="mutable_snapshot_freshness_missing")
        expires_at = observation.observed_at + timedelta(seconds=observation.max_age_seconds)
    else:
        if observation.max_age_seconds is not None:
            return AdapterResult(ObservationStatus.invalid_evidence, reason="immutable_fact_must_not_expire")
        expires_at = None

    payload = {
        **observation.facts,
        "adapter_contract": "claim-action-evidence/v1",
        "fact_mutability": observation.fact_mutability.value,
        "provider_response_id": observation.provider_response_id,
        "bindings": observation.bindings,
    }
    try:
        row = record_evidence(
            db,
            owner_id=context.owner_id,
            execution_id=context.execution_id,
            subject_key=context.subject_key,
            action_key=context.action_key,
            source_type=observation.source_type,
            source_ref=observation.source_ref,
            payload=payload,
            artifact_sha=observation.bindings.get("sha"),
            observed_at=observation.observed_at,
            expires_at=expires_at,
            recorded_by=f"authoritative-adapter:{observation.source_type}",
        )
    except ClaimActionIntegrityError as exc:
        return AdapterResult(ObservationStatus.invalid_evidence, reason=str(exc), bindings=observation.bindings)
    return AdapterResult(ObservationStatus.observed, evidence_id=row.id, bindings=observation.bindings)


def provider_failure(exc: Exception) -> AdapterResult:
    if isinstance(exc, AdapterRateLimited):
        return AdapterResult(ObservationStatus.retryable, retryable=True, reason="provider_rate_limited")
    if isinstance(exc, AdapterAuthenticationError):
        return AdapterResult(ObservationStatus.unavailable, reason="provider_authentication_failed")
    if isinstance(exc, MalformedProviderResponse):
        return AdapterResult(ObservationStatus.invalid_evidence, reason="malformed_provider_response")
    return AdapterResult(ObservationStatus.unknown, retryable=True, reason="provider_unavailable")
