"""MainAI V2 -- Privacy Boundary Engine: the pipeline itself.

LOCAL DATA -> classify -> minimize -> generalize -> policy -> egressable signal.

Fails CLOSED on any stage exception or ambiguity -- exactly like app.egress_policy's existing
enforce_egress_policy() posture (a malformed/uncertain request is denied outright, never
partially sent). No fallback anywhere in this module sends raw or partially-processed content
if a later stage fails; a failure at any stage means nothing is sent, full stop.

OFF mode is enforced BELOW classify(), not at the egress step: run_privacy_pipeline() returns
immediately for OFF without ever calling classify() -- there is no in-memory object anywhere
representing "this signal, prepared to leave," however briefly. See
docs/mainai_v2/MAINAI_V2_PRIVACY_BOUNDARY_ENGINE.md section 3.

VAULT/SECRET/NEVER_EGRESS classifications are a hard, structural block in ALL modes -- see
types.py's _HARD_BLOCK_CLASSIFICATIONS. There is deliberately no owner-authorized VAULT
disclosure path implemented here; building one is explicitly out of scope for this
foundation, not merely unfinished.
"""

from __future__ import annotations

import uuid
from typing import Callable

from app.privacy_boundary.receipts import ReceiptLog
from app.privacy_boundary.sanitize import KNOWN_SAFE_MAX_UNMATCHED_LEN, sanitize_text
from app.privacy_boundary.types import (
    _HARD_BLOCK_CLASSIFICATIONS,
    ClassifiedSignal,
    DataClassification,
    EgressableSignal,
    GeneralizedSignal,
    MinimizedSignal,
    OutboundPurpose,
    PrivacyPolicyDecision,
    RawLocalSignal,
    TelemetryMode,
)

# PURPOSE != AUTHORITY, but purpose still bounds what a MODE is willing to carry -- this is a
# mode-side restriction, not a classification-widening one. MINIMAL intentionally excludes
# LEARNING_SIGNAL: "only non-identifying technical health/error categories" per the design doc.
_MODE_ALLOWED_PURPOSES: dict[TelemetryMode, frozenset[OutboundPurpose]] = {
    TelemetryMode.MINIMAL: frozenset({OutboundPurpose.SYSTEM_HEALTH, OutboundPurpose.CRASH_DIAGNOSTIC}),
    TelemetryMode.LEARNING: frozenset(
        {
            OutboundPurpose.SYSTEM_HEALTH,
            OutboundPurpose.CRASH_DIAGNOSTIC,
            OutboundPurpose.LEARNING_SIGNAL,
            OutboundPurpose.SECURITY_SIGNAL,
            OutboundPurpose.KNOWLEDGE_GAP,
        }
    ),
    TelemetryMode.RESEARCH_OPT_IN: frozenset(
        {
            OutboundPurpose.SYSTEM_HEALTH,
            OutboundPurpose.CRASH_DIAGNOSTIC,
            OutboundPurpose.LEARNING_SIGNAL,
            OutboundPurpose.SECURITY_SIGNAL,
            OutboundPurpose.KNOWLEDGE_GAP,
            OutboundPurpose.RESEARCH_OPT_IN,
        }
    ),
}

MINIMUM_COHORT_SIZE = 5  # implementation-time placeholder, see design doc section 4.


class PrivacyBoundaryError(RuntimeError):
    """Raised internally to trigger the fail-closed path; callers see None from
    run_privacy_pipeline(), never this exception (it is always caught at the top level)."""


def _looks_like_unknown_personal_blob(value: str, matched_categories: tuple[str, ...]) -> bool:
    """A residual string that sanitize_text() found NOTHING structural in, but which is long
    free text, is treated as an unknown personal blob and blocked -- fail closed on the
    unmatched case rather than assuming "no known pattern matched" means "safe." """
    return not matched_categories and len(value) > KNOWN_SAFE_MAX_UNMATCHED_LEN


def classify(signal: RawLocalSignal) -> ClassifiedSignal:
    """Stage 1. `domain` must already be a controlled category string, not free text -- if it
    contains PII-shaped content or looks like an unknown personal blob, classification fails
    closed (raises) rather than silently carrying tainted text into ClassifiedSignal, which
    would then propagate into every later stage since nothing downstream re-checks domain."""
    sanitized_domain, domain_categories = sanitize_text(signal.domain)
    if domain_categories or _looks_like_unknown_personal_blob(signal.domain, domain_categories):
        raise PrivacyBoundaryError(f"domain field is not a clean category string: {signal.domain[:0]}<redacted>")

    return ClassifiedSignal(
        domain=signal.domain,
        skill=None,
        outcome_class=None,
        classification=signal.classification,
        flags=signal.flags,
    )


def minimize(
    classified: ClassifiedSignal,
    *,
    module: str,
    software_version: str,
    agent_version: str | None,
    knowledge_pack_version: str | None,
    skill: str | None = None,
    outcome_class: str | None = None,
    failure_class: str | None = None,
    resolution_class: str | None = None,
    success: bool | None = None,
    latency_bucket: str | None = None,
    confidence_bucket: str | None = None,
    missing_capability_class: str | None = None,
) -> MinimizedSignal:
    """Stage 2. Every argument here is a caller-supplied CATEGORY value, never raw content --
    the type signature has no field capable of holding free text. Any caller trying to pass
    something PII-shaped through one of these category slots is validated below."""
    for label, value in (
        ("skill", skill),
        ("failure_class", failure_class),
        ("resolution_class", resolution_class),
        ("missing_capability_class", missing_capability_class),
    ):
        if value is None:
            continue
        _, categories = sanitize_text(value)
        if categories or _looks_like_unknown_personal_blob(value, categories):
            raise PrivacyBoundaryError(f"{label} must be a clean category value, not free text")

    return MinimizedSignal(
        module=module,
        domain=classified.domain,
        skill=skill,
        failure_class=failure_class,
        software_version=software_version,
        agent_version=agent_version,
        knowledge_pack_version=knowledge_pack_version,
        resolution_class=resolution_class,
        success=success,
        latency_bucket=latency_bucket,
        confidence_bucket=confidence_bucket,
        missing_capability_class=missing_capability_class,
    )


def generalize(
    minimized: MinimizedSignal,
    *,
    cohort_size_lookup: Callable[[str, str | None], int | None] | None = None,
) -> GeneralizedSignal:
    """Stage 3. Fail-closed cohort check: UNKNOWN SAFETY STATE != SAFE, applied to privacy --
    if cohort size can't be confirmed (no lookup provided, lookup returns None, or lookup
    raises), treat as below threshold and suppress."""
    suppressed = True
    if cohort_size_lookup is not None:
        try:
            size = cohort_size_lookup(minimized.domain, minimized.skill)
        except Exception:
            size = None
        if size is not None and size >= MINIMUM_COHORT_SIZE:
            suppressed = False

    return GeneralizedSignal(signal=minimized, suppressed=suppressed)


def evaluate_policy(
    classified: ClassifiedSignal,
    *,
    mode: TelemetryMode,
    purpose: OutboundPurpose,
) -> PrivacyPolicyDecision:
    """Stage 4-5. Hard-block classifications deny unconditionally, in every mode -- checked
    FIRST, before any mode/purpose logic, so nothing below can accidentally short-circuit it."""
    if classified.classification in _HARD_BLOCK_CLASSIFICATIONS:
        return PrivacyPolicyDecision(
            allowed=False,
            telemetry_mode_applied=mode,
            reason=f"classification {classified.classification.value} is a hard structural block",
            requires_explicit_owner_opt_in=False,
        )

    if mode == TelemetryMode.OFF:
        # Should be unreachable via run_privacy_pipeline() (OFF returns before classify()),
        # but evaluate_policy() itself still denies defensively if ever called directly.
        return PrivacyPolicyDecision(
            allowed=False, telemetry_mode_applied=mode, reason="OFF mode: no optional telemetry", requires_explicit_owner_opt_in=False
        )

    allowed_purposes = _MODE_ALLOWED_PURPOSES.get(mode, frozenset())
    if purpose not in allowed_purposes:
        return PrivacyPolicyDecision(
            allowed=False,
            telemetry_mode_applied=mode,
            reason=f"purpose {purpose.value} not permitted at telemetry mode {mode.value}",
            requires_explicit_owner_opt_in=(mode != TelemetryMode.RESEARCH_OPT_IN),
        )

    return PrivacyPolicyDecision(allowed=True, telemetry_mode_applied=mode, reason="passed privacy policy", requires_explicit_owner_opt_in=False)


def run_privacy_pipeline(
    signal: RawLocalSignal,
    *,
    mode: TelemetryMode,
    purpose: OutboundPurpose,
    module: str,
    software_version: str,
    agent_version: str | None = None,
    knowledge_pack_version: str | None = None,
    skill: str | None = None,
    outcome_class: str | None = None,
    failure_class: str | None = None,
    resolution_class: str | None = None,
    success: bool | None = None,
    latency_bucket: str | None = None,
    confidence_bucket: str | None = None,
    missing_capability_class: str | None = None,
    cohort_size_lookup: Callable[[str, str | None], int | None] | None = None,
    destination_class: str = "central_learning_aggregator",
    policy_version: int = 1,
    receipt_log: ReceiptLog | None = None,
) -> EgressableSignal | None:
    """The single entry point. Returns the EgressableSignal if -- and only if -- the signal
    is allowed to leave the device; returns None (and records a denial receipt) otherwise.
    Never raises to the caller -- every failure mode is captured and recorded as a denial,
    matching the design doc's fail-closed posture."""
    receipt_log = receipt_log if receipt_log is not None else ReceiptLog.default()

    if mode == TelemetryMode.OFF:
        receipt_log.record(
            owner_id=signal.owner_id,
            purpose=purpose,
            input_classification=signal.classification,
            removed_flag_categories=(),
            output_schema="none",
            policy_version=policy_version,
            destination_class=destination_class,
            decision="denied",
            reason="OFF mode: no optional telemetry -- signal never classified",
        )
        return None

    try:
        classified = classify(signal)
        minimized = minimize(
            classified,
            module=module,
            software_version=software_version,
            agent_version=agent_version,
            knowledge_pack_version=knowledge_pack_version,
            skill=skill,
            outcome_class=outcome_class,
            failure_class=failure_class,
            resolution_class=resolution_class,
            success=success,
            latency_bucket=latency_bucket,
            confidence_bucket=confidence_bucket,
            missing_capability_class=missing_capability_class,
        )
        generalized = generalize(minimized, cohort_size_lookup=cohort_size_lookup)
        decision = evaluate_policy(classified, mode=mode, purpose=purpose)
    except Exception as exc:  # fail closed on ANY stage exception, never partially send
        receipt_log.record(
            owner_id=signal.owner_id,
            purpose=purpose,
            input_classification=signal.classification,
            removed_flag_categories=(),
            output_schema="none",
            policy_version=policy_version,
            destination_class=destination_class,
            decision="denied",
            reason=f"pipeline error, failed closed: {type(exc).__name__}",
        )
        return None

    if not decision.allowed:
        receipt_log.record(
            owner_id=signal.owner_id,
            purpose=purpose,
            input_classification=signal.classification,
            removed_flag_categories=tuple(f.value for f in classified.flags),
            output_schema="none",
            policy_version=policy_version,
            destination_class=destination_class,
            decision="denied",
            reason=decision.reason,
        )
        return None

    if generalized.suppressed:
        receipt = receipt_log.record(
            owner_id=signal.owner_id,
            purpose=purpose,
            input_classification=signal.classification,
            removed_flag_categories=tuple(f.value for f in classified.flags) + ("domain", "skill"),
            output_schema="suppressed",
            policy_version=policy_version,
            destination_class=destination_class,
            decision="allowed",
            reason="cohort size unknown/below threshold: domain/skill suppressed",
        )
        payload = {"outcome_class": outcome_class, "agent_version": agent_version}
        return EgressableSignal(payload=payload, receipt_id=receipt.id)

    payload = {
        "module": minimized.module,
        "domain": minimized.domain,
        "skill": minimized.skill,
        "failure_class": minimized.failure_class,
        "software_version": minimized.software_version,
        "agent_version": minimized.agent_version,
        "knowledge_pack_version": minimized.knowledge_pack_version,
        "resolution_class": minimized.resolution_class,
        "success": minimized.success,
        "latency_bucket": minimized.latency_bucket,
        "confidence_bucket": minimized.confidence_bucket,
        "missing_capability_class": minimized.missing_capability_class,
    }
    receipt = receipt_log.record(
        owner_id=signal.owner_id,
        purpose=purpose,
        input_classification=signal.classification,
        removed_flag_categories=tuple(f.value for f in classified.flags),
        output_schema="MinimizedSignal",
        policy_version=policy_version,
        destination_class=destination_class,
        decision="allowed",
        reason="passed privacy policy",
    )
    return EgressableSignal(payload=payload, receipt_id=receipt.id)
