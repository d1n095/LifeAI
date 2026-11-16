"""MainAI V2 -- Privacy Boundary Engine (Stage V2-C): core types.

Standalone, non-imported foundation -- not wired into any production runtime path yet.
See docs/mainai_v2/MAINAI_V2_PRIVACY_BOUNDARY_ENGINE.md for the design this implements.

Every stage's dataclass is frozen and structurally cannot carry more than its own stage's
allowed fields -- RawLocalSignal.raw_content never appears on anything past ClassifiedSignal,
by construction, not by convention. This is the same discipline app.egress_policy already
uses for provider-planning egress; this module generalizes it to every outbound signal and
adds the classify/minimize/generalize stages that sit before egress_policy's own redaction
step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TelemetryMode(str, Enum):
    OFF = "OFF"
    MINIMAL = "MINIMAL"
    LEARNING = "LEARNING"
    RESEARCH_OPT_IN = "RESEARCH_OPT_IN"


class DataClassification(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    PRIVATE = "PRIVATE"
    CONFIDENTIAL = "CONFIDENTIAL"
    VAULT = "VAULT"
    SECRET = "SECRET"
    NEVER_EGRESS = "NEVER_EGRESS"


# Classifications that are a hard, structural block -- no code path in this package can
# override these. VAULT is included: today there is no owner-authorized VAULT disclosure
# path, so VAULT always blocks (do not build a bypass "for future use" -- see module docstring
# in pipeline.py).
_HARD_BLOCK_CLASSIFICATIONS = frozenset(
    {DataClassification.SECRET, DataClassification.NEVER_EGRESS, DataClassification.VAULT}
)


class DataFlag(str, Enum):
    """Orthogonal to DataClassification -- a signal can carry several of these at once."""

    IDENTIFYING = "IDENTIFYING"
    FINANCIAL = "FINANCIAL"
    HEALTH = "HEALTH"
    LOCATION = "LOCATION"
    COMMUNICATION = "COMMUNICATION"
    DOCUMENT_CONTENT = "DOCUMENT_CONTENT"
    AUTH_SECRET = "AUTH_SECRET"
    DEVICE_IDENTIFIER = "DEVICE_IDENTIFIER"
    BEHAVIORAL_PROFILE = "BEHAVIORAL_PROFILE"
    IP_PROTECTED = "IP_PROTECTED"


class OutboundPurpose(str, Enum):
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    CRASH_DIAGNOSTIC = "CRASH_DIAGNOSTIC"
    LEARNING_SIGNAL = "LEARNING_SIGNAL"
    SECURITY_SIGNAL = "SECURITY_SIGNAL"
    KNOWLEDGE_GAP = "KNOWLEDGE_GAP"
    RESEARCH_OPT_IN = "RESEARCH_OPT_IN"
    SUPPORT_EXPORT = "SUPPORT_EXPORT"
    PROVIDER_CONTEXT = "PROVIDER_CONTEXT"


# PURPOSE != AUTHORITY: this table intentionally does NOT map purpose -> allowed
# classification. Purpose and classification are checked independently by the pipeline;
# declaring a purpose never widens what classification level may egress. See pipeline.py's
# evaluate_policy().


@dataclass(frozen=True)
class RawLocalSignal:
    """Stage 0 input -- exactly what MainAI/a department observed locally. NEVER leaves this
    dataclass's own scope; the pipeline drops raw_content before ClassifiedSignal exists."""

    owner_id: uuid.UUID
    domain: str
    raw_content: Any
    classification: DataClassification
    flags: frozenset[DataFlag] = field(default_factory=frozenset)
    source: str = "unknown"


@dataclass(frozen=True)
class ClassifiedSignal:
    """Stage 1 output -- what KIND of thing this is, never what it says. Structurally has no
    field that could hold RawLocalSignal.raw_content."""

    domain: str
    skill: str | None
    outcome_class: str | None
    classification: DataClassification
    flags: frozenset[DataFlag]


@dataclass(frozen=True)
class MinimizedSignal:
    """Stage 2 -- reduced to the smallest fact set that still lets central learn something
    about the software. This is the ONLY dataclass that may cross the process boundary,
    always wrapped in EgressableSignal for the actual send."""

    module: str
    domain: str
    skill: str | None
    failure_class: str | None
    software_version: str
    agent_version: str | None
    knowledge_pack_version: str | None
    resolution_class: str | None
    success: bool | None
    latency_bucket: str | None
    confidence_bucket: str | None
    missing_capability_class: str | None


@dataclass(frozen=True)
class GeneralizedSignal:
    """Stage 3 -- MinimizedSignal coarsened further where domain/skill values themselves
    could be identifying at small scale, plus suppression when cohort size is unknown/small."""

    signal: MinimizedSignal
    suppressed: bool


@dataclass(frozen=True)
class PrivacyPolicyDecision:
    """Stage 4-5 -- policy + local approval."""

    allowed: bool
    telemetry_mode_applied: TelemetryMode
    reason: str
    requires_explicit_owner_opt_in: bool


@dataclass(frozen=True)
class EgressableSignal:
    """Stage 6 output -- the ONLY thing that may ever leave the device for this signal."""

    payload: dict[str, Any]
    receipt_id: uuid.UUID


@dataclass(frozen=True)
class PrivacyReceipt:
    """Immutable local audit record for every egress decision (allowed or denied). NEVER
    contains the raw secret/content that was removed -- only categories/counts."""

    id: uuid.UUID
    owner_id: uuid.UUID
    purpose: OutboundPurpose
    input_classification: DataClassification
    removed_flag_categories: tuple[str, ...]
    output_schema: str
    policy_version: int
    timestamp: datetime
    destination_class: str
    decision: str  # "allowed" | "denied"
    reason: str

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
