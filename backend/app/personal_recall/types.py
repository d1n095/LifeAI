from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    CONVERSATION = "conversation"
    DURABLE_MEMORY = "durable_memory"
    FILE = "file"
    NOTE = "note"
    PROJECT = "project"
    INTENT = "intent"
    WORKSPACE_MEMORY = "workspace_memory"
    PREVIOUS_ANSWER = "previous_answer"
    RESEARCH_NOTE = "research_note"
    DECISION_RECORD = "decision_record"
    LINKED_EVIDENCE = "linked_evidence"
    KNOWLEDGE_PACK = "knowledge_pack"


class QueryIntent(str, Enum):
    BROAD_RECALL = "broad_recall"
    LATEST_STATE = "latest_state"
    TIMELINE = "timeline"
    CHANGE_HISTORY = "change_history"
    DECISION_HISTORY = "decision_history"
    SOURCE_LOOKUP = "source_lookup"
    FILE_LOOKUP = "file_lookup"
    CONTRADICTION_SEARCH = "contradiction_search"
    VERSION_COMPARE = "version_compare"
    WHY_CHANGED = "why_changed"
    SHOW_EVIDENCE = "show_evidence"


class DecisionState(str, Enum):
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


class VerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class IndexState(str, Enum):
    DISCOVERED = "discovered"
    PARSED = "parsed"
    INDEXED = "indexed"
    PARTIAL = "partial"
    STALE = "stale"
    FAILED = "failed"
    DELETED = "deleted"
    SUPERSEDED = "superseded"


class CompletenessState(str, Enum):
    COMPLETE = "complete"
    KNOWN_PARTIAL = "known_partial"
    UNKNOWN = "unknown"


class AliasVerification(str, Enum):
    OBSERVED = "observed"
    VERIFIED = "verified"
    REJECTED = "rejected"


class SourceAuthority(str, Enum):
    PRIMARY = "primary"
    USER = "user"
    VERIFIED_DERIVED = "verified_derived"
    DERIVED = "derived"
    ASSISTANT = "assistant"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AliasBinding:
    canonical: str
    alias: str
    owner_id: str
    verification: AliasVerification
    project_id: str | None = None
    domain: str | None = None
    subject: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_role: str = "unknown"


@dataclass(frozen=True)
class QueryInterpretationProposal:
    """Future model output: inspectable proposal, never retrieval authority by itself."""

    intents: tuple[QueryIntent, ...]
    subject: str | None
    aliases: tuple[str, ...] = ()
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True, repr=False)
class Provenance:
    source_type: SourceType
    source_id: str
    locator: str
    section: str | None = None
    conversation_id: str | None = None
    file_id: str | None = None
    version: str | None = None
    occurred_at: datetime | None = None


@dataclass(repr=False)
class PersonalKnowledgeItem:
    item_id: str
    source_type: SourceType
    source_id: str
    owner_id: str
    content_reference: str
    provenance: Provenance
    text: str | None = field(default=None, repr=False)
    conversation_id: str | None = None
    thread_id: str | None = None
    project_id: str | None = None
    file_id: str | None = None
    intent_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    subject: str | None = None
    topic: str | None = None
    entities: tuple[str, ...] = ()
    claims: tuple[str, ...] = field(default=(), repr=False)
    aliases: tuple[str, ...] = field(default=(), repr=False)
    decision_state: DecisionState = DecisionState.UNKNOWN
    verification_state: VerificationState = VerificationState.UNKNOWN
    superseded_by: str | None = None
    source_version: str | None = None
    embedding_reference: str | None = None
    privacy_class: str = "local_personal"
    source_authority: SourceAuthority = SourceAuthority.UNKNOWN
    confidence: float | None = None
    currentness: float | None = None
    relationship_edges: dict[str, tuple[str, ...]] = field(default_factory=dict)
    index_state: IndexState = IndexState.INDEXED
    content_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, repr=False)
class RecallQuery:
    raw: str
    normalized: str
    terms: tuple[str, ...]
    intents: tuple[QueryIntent, ...]
    subject: str | None = None
    aliases: tuple[str, ...] = ()
    project_id: str | None = None
    source_types: tuple[SourceType, ...] = ()
    include_historical: bool = True
    current_only: bool = False


@dataclass(repr=False)
class RetrievalResult:
    item: PersonalKnowledgeItem
    relevance_score: float
    exact_score: float
    semantic_score: float
    entity_score: float
    temporal_score: float
    authority_score: float
    truth_score: float
    subject_match: bool
    why_matched: tuple[str, ...]
    contradictions: tuple[str, ...] = ()
    superseded_by: str | None = None
    related_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContradictionCandidate:
    left_item_id: str
    right_item_id: str
    proposition: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class CoverageReport:
    state: CompletenessState
    searched_source_types: tuple[SourceType, ...]
    missing_source_types: tuple[SourceType, ...]
    failed_adapters: tuple[str, ...] = ()
    truncated: bool = False
    source_states: tuple[tuple[SourceType, CompletenessState], ...] = ()


@dataclass(repr=False)
class RecallResponse:
    query: RecallQuery
    results: list[RetrievalResult]
    clusters: dict[str, list[str]]
    current_items: list[str]
    historical_items: list[str]
    contradictions: list[tuple[str, str]]
    contradiction_candidates: list[ContradictionCandidate]
    unresolved: list[str]
    index_warnings: list[str]
    coverage: CoverageReport
    synthesis: str
