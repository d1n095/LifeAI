"""Shared vocabulary for `app.mainai_coverage`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md for the architecture
decision.

Re-exports `app.mainai_vision.types.MaturityState`/`MATURITY_ORDER`/`MATURITY_INDEX`/
`maturity_at_least` VERBATIM -- the founder's own maturity ladder (MENTIONED -> ... ->
PRODUCTION_PROVEN) is already implemented there as the 13-rung ordinal `MaturityState`
(`DISCOVERED` is that ladder's own name for the founder's "MENTIONED" rung;
`ROBUSTNESS_TESTED` is its name for "ROBUSTNESS_REVIEWED" -- same rung, pre-existing name,
never renamed/duplicated). `CoverageDisposition` below is a NEW, orthogonal axis for the four
states the ordinal ladder does not carry (SUPERSEDED/REJECTED/BLOCKED/UNACCOUNTED_FOR) --
mirrors `app.mainai_research.types.EvidenceLifecycleStatus` being a separate axis from
`EvidenceState` rather than a competing ladder.

MENTIONED != IMPLEMENTED. IMPLEMENTED != VERIFIED. VERIFIED != INTEGRATED. INTEGRATED !=
ACTIVATED. ACTIVATED != PRODUCTION_PROVEN. ABSENT_FROM_SUMMARY != NEVER_DISCLOSED.
NOT_IN_ROADMAP != INTENTIONALLY_REJECTED. NO_KNOWN_GAP != COMPLETE."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from app.mainai_vision.types import (  # re-export, never duplicated
    MATURITY_INDEX,
    MATURITY_ORDER,
    MaturityState,
    maturity_at_least,
)

__all__ = [
    "MaturityState",
    "MATURITY_ORDER",
    "MATURITY_INDEX",
    "maturity_at_least",
    "CoverageDisposition",
    "TraceabilityStage",
    "TRACEABILITY_ORDER",
    "CapabilityClaim",
    "CoverageError",
]


class CoverageError(ValueError):
    """Raised on a structural/ownership violation, never for "no data observed yet"."""


class CoverageDisposition(str, enum.Enum):
    """Terminal/exceptional states the ordinal `MaturityState` ladder does not carry.
    NOT_IN_ROADMAP != INTENTIONALLY_REJECTED: the ABSENCE of a disposition is never read as
    REJECTED -- only an explicit `REJECTED` disposition means that."""

    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    UNACCOUNTED_FOR = "unaccounted_for"


class TraceabilityStage(str, enum.Enum):
    """VISION -> REQUIREMENT -> WHY -> ARCHITECTURE -> IMPLEMENTATION -> TEST -> REVIEW ->
    INTEGRATION -> ACTIVATION -> RUNTIME_EVIDENCE, ordinal, per the founder's own §2."""

    VISION = "vision"
    REQUIREMENT = "requirement"
    WHY = "why"
    ARCHITECTURE = "architecture"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    REVIEW = "review"
    INTEGRATION = "integration"
    ACTIVATION = "activation"
    RUNTIME_EVIDENCE = "runtime_evidence"


TRACEABILITY_ORDER: tuple[TraceabilityStage, ...] = tuple(TraceabilityStage)


@dataclass(frozen=True)
class CapabilityClaim:
    """One caller-supplied assertion that some capability was discussed/built/tested/reviewed
    somewhere in the project's history. This package NEVER scans conversation history, commits,
    or docs itself -- it has no NLP/LLM call anywhere (matching `mainai_vision.gap_generator`'s
    own "deterministic keyword map, never an LLM judgment call" doctrine) -- a caller (a future
    corpus-reading orchestrator, or a human/agent doing the reading) supplies these claims as
    real, disclosed input. `mention_count`/`stages_present`/`disposition` are the caller's own
    honest findings, never invented here."""

    claim_id: str
    description: str
    mention_count: int = 0
    stages_present: frozenset[TraceabilityStage] = field(default_factory=frozenset)
    maturity: MaturityState | None = None
    disposition: CoverageDisposition | None = None
    has_runtime_evidence: bool = False
    last_seen_at: datetime | None = None
    provenance: tuple[str, ...] = field(default_factory=tuple)
