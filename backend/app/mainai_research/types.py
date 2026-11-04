"""Shared vocabulary for `app.mainai_research`. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the full architecture
decision this package implements.

SOURCE OF TRUTH != SOURCE OF AUTHORITY. RESEARCH != AUTHORITY. CONCLUSION != EXECUTION AUTHORITY.

Re-exports `app.mainai_vision.evidence.EvidenceState`/`RawEvidence`/`count_independent_sources`
verbatim -- this package's own evidence-lifecycle vocabulary (`EvidenceLifecycleStatus`) is a
DIFFERENT, orthogonal axis (an evidence item's standing WITHIN one investigation, e.g. rejected/
reopened/superseded) composed WITH the existing evidential-directness axis, never a replacement
for it."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.mainai_vision.evidence import (  # re-export, never duplicated
    EvidenceState,
    RawEvidence,
    count_independent_sources,
)

__all__ = [
    "EvidenceState",
    "RawEvidence",
    "count_independent_sources",
    "EvidenceLifecycleStatus",
    "EvidenceRole",
    "HypothesisStatus",
    "InvestigationStatus",
    "SpecialistRole",
    "CausalTest",
    "ProviderRecommendation",
    "KnowledgeItemState",
    "Actor",
    "Relationship",
    "MoneyFlow",
    "TimelineEvent",
    "KnowledgeItem",
    "ResearchError",
]


class ResearchError(ValueError):
    """Raised on a structural/ownership violation. Never raised for "no data was observed" --
    that stays a `EvidenceLifecycleStatus.UNRESOLVED`/`INSUFFICIENT_EVIDENCE` row, never an
    exception."""


class EvidenceRole(str, enum.Enum):
    SUPPORT = "support"
    CONTRADICTION = "contradiction"


class EvidenceLifecycleStatus(str, enum.Enum):
    """An evidence item's standing WITHIN one investigation -- never collapsed together (§10).
    REJECTED_AS_SUPPORT != PROVEN_FALSE. REJECTED EVIDENCE != DELETED EVIDENCE (a rejected row is
    never deleted, only re-classified; see `research_ledger.reopen_evidence()`)."""

    REJECTED_AS_SUPPORT = "rejected_as_support"
    PROVEN_FALSE = "proven_false"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"
    DEPRIORITIZED = "deprioritized"
    UNRESOLVED = "unresolved"
    STALE = "stale"
    VALID_SUPPORT = "valid_support"
    STRONG_SUPPORT = "strong_support"
    VERIFIED_WHERE_POSSIBLE = "verified_where_possible"


class HypothesisStatus(str, enum.Enum):
    ACTIVE = "active"
    SURVIVED_FALSIFICATION = "survived_falsification"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class InvestigationStatus(str, enum.Enum):
    ACTIVE = "active"
    SATURATED_FOR_NOW = "saturated_for_now"  # SATURATED_FOR_NOW != PERMANENTLY CLOSED
    CLOSED = "closed"


class SpecialistRole(str, enum.Enum):
    """A REASONING PERSPECTIVE over one body of evidence -- deliberately distinct from
    `app.agent_coordination.WorkAssignmentRole` (a staffing/dispatch role for who does the
    actual work) and from `app.strategy_evaluation`'s "challenger" (a competing WORK STRATEGY
    execution, not a perspective). No two enums share a value."""

    MAINAI = "mainai"
    ANALYST = "analyst"
    ECONOMIST = "economist"
    LEGAL_COUNSEL = "legal_counsel"
    ADVERSARIAL_COUNSEL = "adversarial_counsel"
    EVIDENCE_ANALYST = "evidence_analyst"


class CausalTest(str, enum.Enum):
    CORRELATION = "correlation"
    COMMON_CAUSE = "common_cause"
    SELECTION_EFFECT = "selection_effect"
    REVERSE_CAUSATION = "reverse_causation"
    COINCIDENCE = "coincidence"
    HIDDEN_VARIABLE = "hidden_variable"
    MECHANISM = "mechanism"
    TEMPORAL_ORDER = "temporal_order"
    COUNTERFACTUAL = "counterfactual"
    FALSIFICATION = "falsification"


class ProviderRecommendation(str, enum.Enum):
    KEEP = "keep"
    REMOVE = "remove"
    PAUSE = "pause"
    LIMIT_TO_SPECIFIC_TASKS = "limit_to_specific_tasks"
    DOWNGRADE = "downgrade"
    UPGRADE = "upgrade"
    CHANGE_DEFAULT_MODEL = "change_default_model"
    CHANGE_PROVIDER = "change_provider"
    KEEP_AS_FALLBACK = "keep_as_fallback"
    TRIAL = "trial"
    ADD = "add"
    RENEGOTIATE = "renegotiate"


class KnowledgeItemState(str, enum.Enum):
    """The founder's own named knowledge-representation ladder -- deliberately similar in
    spirit to (never imported from) Codex's Personal Recall `DecisionState`, which lives on a
    separate, unmerged branch not present in this checkout. "I think X may be useful" (MENTION/
    IDEA) must never silently become "founder permanently decided X" (DECISION) -- see
    `knowledge_ingestion.py` for the structural transition gate."""

    MENTION = "mention"
    IDEA = "idea"
    ASSUMPTION = "assumption"
    CLAIM = "claim"
    DECISION = "decision"
    PLAN = "plan"
    IMPLEMENTATION = "implementation"
    VERIFIED_RESULT = "verified_result"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    kind: str  # "person" | "organization"
    notes: str | None = None


@dataclass(frozen=True)
class Relationship:
    """RELATIONSHIP != CONTROL: this dataclass records an evidenced tie, never a claim of
    control or intent -- `kind` is a plain descriptive label (e.g. "employed_by",
    "co_authored", "met_with"), never "controls"/"conspires_with"."""

    from_actor_id: str
    to_actor_id: str
    kind: str
    evidenced_by: tuple[str, ...] = field(default_factory=tuple)  # evidence_link ids
    notes: str | None = None


@dataclass(frozen=True)
class MoneyFlow:
    """BENEFIT != PROOF OF INTENT: a recorded, evidenced financial relationship only."""

    from_actor_id: str
    to_actor_id: str
    flow_kind: str  # "funding" | "payment" | "ownership" | "contract" | "beneficiary"
    amount: float | None = None
    currency: str | None = None
    evidenced_by: tuple[str, ...] = field(default_factory=tuple)
    notes: str | None = None


@dataclass(frozen=True)
class TimelineEvent:
    occurred_at: datetime
    description: str
    actor_ids: tuple[str, ...] = field(default_factory=tuple)
    evidenced_by: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class KnowledgeItem:
    item_id: str
    owner_id: str
    what: str
    who: str | None
    when: datetime | None
    context: str | None
    source: str
    original_vs_derived: str  # "original" | "derived"
    state: KnowledgeItemState
    currentness: str  # "current" | "superseded" | "stale"
    confidence: float | None
    inference_vs_explicit: str  # "explicit_text" | "inference"
    supersedes_item_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
