"""Provider-neutral, read-only observation contract for integrity evidence adapters."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


class FactMutability(str, enum.Enum):
    immutable_fact = "immutable_fact"
    mutable_snapshot = "mutable_snapshot"


class ObservationStatus(str, enum.Enum):
    observed = "observed"
    unknown = "unknown"
    invalid_evidence = "invalid_evidence"
    unavailable = "unavailable"
    retryable = "retryable"


@dataclass(frozen=True)
class EvidenceContext:
    owner_id: UUID
    execution_id: str
    subject_key: str
    action_key: str
    task_id: str | None = None


@dataclass(frozen=True)
class ProviderObservation:
    source_type: str
    source_ref: str
    provider_response_id: str
    fact_mutability: FactMutability
    bindings: dict[str, str]
    facts: dict[str, Any]
    observed_at: datetime
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class AdapterResult:
    status: ObservationStatus
    evidence_id: UUID | None = None
    retryable: bool = False
    reason: str | None = None
    bindings: dict[str, str] = field(default_factory=dict)
