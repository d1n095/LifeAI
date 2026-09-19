"""Shared vocabulary for `app.mainai_vision` -- see
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the full architecture
decision this package implements.

VISION != AUTHORITY. IMPLIED REQUIREMENT != EXECUTION AUTHORITY. MODEL OUTPUT != AUTHORITY.
100% OF CURRENT VISION != NOTHING MORE CAN BE IMPROVED.

Re-exports `app.resource_intelligence.types.MetricEnvelope`/`unknown_metric` verbatim -- this
package's own Statistics Command Center never invents a second metric shape (METRIC != TRUTH,
already structurally enforced there); see `statistics.py`."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.resource_intelligence.types import MetricEnvelope, unknown_metric  # re-export, never duplicated

__all__ = [
    "MetricEnvelope",
    "unknown_metric",
    "MaturityState",
    "MATURITY_ORDER",
    "MATURITY_INDEX",
    "maturity_at_least",
    "VisionNodeKind",
    "VisionEdgeKind",
    "CompletionDimension",
    "EXCLUDED_ENTITY_STATUSES",
    "VisionNode",
    "VisionEdge",
    "VisionGraph",
    "VisionError",
]


class VisionError(ValueError):
    """Raised on a structural/ownership violation -- e.g. an edge referencing a node id this
    graph does not contain. Never raised for "no data was observed" -- that is always an
    honest `unknown_metric()`/`missing_data=True`, never an exception."""


class MaturityState(str, enum.Enum):
    """The 13-state maturity ladder a `VisionNode` occupies -- ordinal, never regressed by this
    package itself (a node's OWN linked evidence can regress it, e.g. a test starts failing;
    this package never fabricates forward progress)."""

    DISCOVERED = "DISCOVERED"
    UNDERSTOOD = "UNDERSTOOD"
    SPECIFIED = "SPECIFIED"
    ARCHITECTED = "ARCHITECTED"
    IMPLEMENTED = "IMPLEMENTED"
    UNIT_TESTED = "UNIT_TESTED"
    INTEGRATION_TESTED = "INTEGRATION_TESTED"
    ROBUSTNESS_TESTED = "ROBUSTNESS_TESTED"
    INDEPENDENTLY_REVIEWED = "INDEPENDENTLY_REVIEWED"
    INTEGRATED = "INTEGRATED"
    ACTIVATED = "ACTIVATED"
    PRODUCTION_PROVEN = "PRODUCTION_PROVEN"
    MAINTAINED = "MAINTAINED"


# Ordinal position -- used only for comparison (>=), never displayed as a metric value in its
# own right (policy vocabulary, matching `resource_intelligence.founder_attention`'s own
# "ordinal, not a metric" convention).
MATURITY_ORDER: tuple[MaturityState, ...] = tuple(MaturityState)
MATURITY_INDEX = {state: i for i, state in enumerate(MATURITY_ORDER)}


def maturity_at_least(actual: MaturityState, threshold: MaturityState) -> bool:
    return MATURITY_INDEX[actual] >= MATURITY_INDEX[threshold]


class VisionNodeKind(str, enum.Enum):
    """The widened `project_entities.entity_type` vocabulary this package uses (migration
    0072) -- `VISION` maps to the pre-existing `vision_statement` value (no new value needed
    there; every other member here is a genuinely new, additive CHECK-constraint value)."""

    VISION = "vision_statement"
    DOMAIN = "domain"
    CAPABILITY = "capability"
    SYSTEM = "system"
    SUBSYSTEM = "subsystem"
    REQUIREMENT = "requirement"
    IMPLIED_REQUIREMENT = "implied_requirement"
    INVARIANT = "invariant"
    RISK = "risk"
    ACCEPTANCE_CRITERION = "acceptance_criterion"
    VERIFICATION_CRITERION = "verification_criterion"


class VisionEdgeKind(str, enum.Enum):
    """The widened `project_entity_relationships.relationship_type` vocabulary this package
    uses (migration 0072) -- `CONTRADICTS`/`DUPLICATES`/`DERIVED_FROM` map to pre-existing
    values already carrying exactly this meaning; `DEPENDS_ON`/`IMPLIES`/`VERIFIES`/`SATISFIES`/
    `MITIGATES` are the new, additive values. Supersession is NOT here -- it stays on
    `ProjectEntity.supersedes_entity_id`/`mark_project_entity_superseded()`, reused unchanged,
    never duplicated as a relationship_type."""

    DEPENDS_ON = "depends_on"
    IMPLIES = "implies"
    VERIFIES = "verifies"
    SATISFIES = "satisfies"
    MITIGATES = "mitigates"
    CONTRADICTS = "contradicts"
    DUPLICATES = "duplicates"
    DERIVED_FROM = "derived_from"


class CompletionDimension(str, enum.Enum):
    """The 11 dimensions `completion.py` weighs completion across -- never task-count based."""

    CAPABILITY = "capability"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PRIVACY = "privacy"
    INTEGRATION = "integration"
    RECOVERY = "recovery"
    VERIFICATION = "verification"
    OPERABILITY = "operability"
    PERFORMANCE = "performance"
    UX = "ux"
    PRODUCTION_READINESS = "production_readiness"


# Statuses this package treats as EXCLUDED from the active vision graph -- mirrors
# `project_entities.service.list_current_project_entities()`'s own "safe by default" convention
# exactly (never re-derived from a different rule).
EXCLUDED_ENTITY_STATUSES = frozenset({"historical", "superseded", "disputed"})


@dataclass(frozen=True)
class VisionNode:
    """One compiled vision-graph node -- a read-only PROJECTION of a real `ProjectEntity` row
    (never a second copy of truth: `entity_id` is the one place to go back and read the real
    row). `maturity`/`confidence`/`evidence_summary` are computed by `completion.py` at compile
    time from real linked signals, never invented here."""

    entity_id: uuid.UUID
    kind: VisionNodeKind
    title: str
    summary: str | None
    status: str
    authority: str
    basis: str
    confidence: float | None
    currentness: str  # "current" | "superseded" | "disputed" | "historical"
    source_claim_id: uuid.UUID | None
    supersedes_entity_id: uuid.UUID | None
    created_at: datetime
    why: str | None = None
    maturity: MaturityState = MaturityState.DISCOVERED
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VisionEdge:
    from_entity_id: uuid.UUID
    to_entity_id: uuid.UUID
    kind: VisionEdgeKind
    note: str | None = None


@dataclass(frozen=True)
class VisionGraph:
    """The compiled, in-memory canonical vision -- never itself durable (see
    `vision_compiler.compile_vision_graph()`'s own docstring: DERIVE, NEVER DUPLICATE). Every
    node here is `current` per `EXCLUDED_ENTITY_STATUSES` -- a caller wanting the full history
    (including superseded/disputed/rejected) queries `app.project_entities` directly, exactly
    like every other "current view" this codebase already builds
    (`list_current_project_entities()`, `list_current_diagnoses()`)."""

    owner_id: uuid.UUID
    compiled_at: datetime
    nodes: tuple[VisionNode, ...]
    edges: tuple[VisionEdge, ...]
    excluded_count: int  # how many non-current rows were filtered out -- disclosed, never hidden

    def node(self, entity_id: uuid.UUID) -> VisionNode | None:
        for n in self.nodes:
            if n.entity_id == entity_id:
                return n
        return None

    def edges_from(self, entity_id: uuid.UUID) -> tuple[VisionEdge, ...]:
        return tuple(e for e in self.edges if e.from_entity_id == entity_id)

    def edges_to(self, entity_id: uuid.UUID) -> tuple[VisionEdge, ...]:
        return tuple(e for e in self.edges if e.to_entity_id == entity_id)

    def nodes_of_kind(self, kind: VisionNodeKind) -> tuple[VisionNode, ...]:
        return tuple(n for n in self.nodes if n.kind == kind)
