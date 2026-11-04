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
NOT_IN_ROADMAP != INTENTIONALLY_REJECTED. NO_KNOWN_GAP != COMPLETE. UNKNOWN != ABSENT.

`SourceKind`/`NormalizedObservation`/`SourceAvailability` back the automatic discovery layer
(`source_adapters.py`/`requirement_extraction.py`) -- REAL durable sources this repo actually
has (handoff docs, reconciliation docs, the branch registry, git commit log) are ingested for
real; a source this repo genuinely cannot access (conversation/chat history -- no durable store
of it exists anywhere in this codebase) is represented explicitly via `SourceAvailability
(available=False, ...)`, never fabricated or silently skipped."""

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
    "SourceKind",
    "ExtractionMethod",
    "NormalizedObservation",
    "SourceAvailability",
    "MatchLayer",
]


class SourceKind(str, enum.Enum):
    """Durable source classes this repo can actually reach. `CONVERSATION_HISTORY` is listed
    because the founder's own program names it as a class to SUPPORT WHERE AVAILABLE -- it is
    always reported via `SourceAvailability(available=False, ...)` on this branch, never
    faked, because no durable store of conversation/chat history exists anywhere in this
    codebase (confirmed by direct inspection, not assumed)."""

    HANDOFF_DOC = "handoff_doc"
    RECONCILIATION_DOC = "reconciliation_doc"
    BRANCH_REGISTRY = "branch_registry"
    GIT_COMMIT_LOG = "git_commit_log"
    ARCHITECTURE_DOC = "architecture_doc"
    TEST_EVIDENCE = "test_evidence"
    CONVERSATION_HISTORY = "conversation_history"


class ExtractionMethod(str, enum.Enum):
    """How a `NormalizedObservation`'s text was pulled out of its source -- deterministic,
    never an LLM judgment call, matching `mainai_vision.gap_generator`'s own doctrine."""

    MARKDOWN_HEADING_SECTION = "markdown_heading_section"
    MARKDOWN_TABLE_ROW = "markdown_table_row"
    GIT_LOG_SUBJECT = "git_log_subject"
    TEST_FILE_SCAN = "test_file_scan"
    UNAVAILABLE = "unavailable"


class MatchLayer(str, enum.Enum):
    """The layered comparison strategy §B requires -- deterministic/local layers are tried in
    this order; `SEMANTIC_SIMILARITY` is NEVER silently claimed: no local/bounded semantic
    model exists in this codebase, so that layer always reports itself unavailable rather than
    quietly falling back to a keyword score and calling it semantic understanding."""

    EXACT_IDENTITY = "exact_identity"
    NORMALIZED_LEXICAL = "normalized_lexical"
    ALIAS = "alias"
    STRUCTURED_LINK = "structured_link"
    GRAPH_RELATIONSHIP = "graph_relationship"
    KEYWORD_OVERLAP = "keyword_overlap"  # the pre-existing Jaccard signal -- one layer among several now, never the whole story
    SEMANTIC_SIMILARITY = "semantic_similarity"  # always reported unavailable on this branch


@dataclass(frozen=True)
class SourceAvailability:
    source_kind: SourceKind
    available: bool
    reason: str


@dataclass(frozen=True)
class NormalizedObservation:
    """One real, provenance-carrying observation pulled from a real durable source.
    `exact_sha` is populated only for git-log-derived observations; `location` is a heading/
    section/table-row identifier within the source; `confidence` reflects how directly the
    extraction method maps to real content (a full section body is higher-confidence than a
    single table-cell fragment) -- never a claim about whether the CONTENT itself is true."""

    source_kind: SourceKind
    source_identifier: str  # file path, or "git:<repo_path>"
    text: str
    location: str | None
    timestamp: datetime | None
    exact_sha: str | None
    extraction_method: ExtractionMethod
    confidence: float
    superseded: bool = False


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
    """One assertion that some capability was discussed/built/tested/reviewed somewhere in the
    project's history. Producible two ways, both real: (1) a caller (human/agent) supplies one
    directly, or (2) `requirement_extraction.extract_capability_claims()` produces one from
    real `NormalizedObservation`s pulled by `source_adapters.py` from real, accessible durable
    sources. Either way this package has no NLP/LLM call anywhere (matching
    `mainai_vision.gap_generator`'s own "deterministic keyword map, never an LLM judgment
    call" doctrine). `mention_count`/`stages_present`/`disposition` are always someone's (a
    caller's or an adapter's) own honest finding, never invented here.

    `source_records` carries full provenance (source/type/identifier/timestamp/exact_sha/
    location/extraction_method/confidence) for every observation that contributed to this
    claim; the older, plain `provenance` string tuple remains for backward compatibility with
    hand-built claims that don't go through the extraction pipeline."""

    claim_id: str
    description: str
    mention_count: int = 0
    stages_present: frozenset[TraceabilityStage] = field(default_factory=frozenset)
    maturity: MaturityState | None = None
    disposition: CoverageDisposition | None = None
    has_runtime_evidence: bool = False
    last_seen_at: datetime | None = None
    provenance: tuple[str, ...] = field(default_factory=tuple)
    source_records: tuple[NormalizedObservation, ...] = field(default_factory=tuple)
