"""Continuous founder↔MainAI conversation vocabulary.

The founder talks to MainAI. MainAI manages machines. Conversation text never grants
merge, deploy, Recall, provider, or RLS authority. Subject binding is required: discovering
"a SHA" is not the same as answering the requested entity.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class TurnDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class InboundKind(str, enum.Enum):
    ORDINARY_CHAT = "ordinary_chat"
    STATUS_QUESTION = "status_question"
    RELAY_REQUEST = "relay_request"
    WORK_COMMAND = "work_command"
    AUTHORITY_REQUEST = "authority_request"


class OutboundDisposition(str, enum.Enum):
    SEND = "send"
    SUPPRESS = "suppress"
    REWRITE = "rewrite"


class InterruptKind(str, enum.Enum):
    NONE = "none"
    FOUNDER_ONLY_AUTHORITY = "founder_only_authority"
    MATERIAL_PRODUCT_DECISION = "material_product_decision"
    SECURITY_POLICY = "security_policy"
    MONEY_BUDGET = "money_budget"
    DESTRUCTIVE_ACTION = "destructive_action"
    UNRESOLVED_ALTERNATIVES = "unresolved_alternatives"
    GENUINE_BLOCKER = "genuine_blocker"


class RelayCategory(str, enum.Enum):
    SHA = "sha"
    BRANCH_NAME = "branch_name"
    TEST_RESULT = "test_result"
    AGENT_MESSAGE = "agent_message"
    BLOCKER = "blocker"
    WAIT_GRAPH = "wait_graph"
    NEXT_AGENT = "next_agent"
    CI_STATE = "ci_state"


class InternalActionKind(str, enum.Enum):
    DISCOVER_FROM_GITHUB = "discover_from_github"
    HOLD_BUSY_AGENT = "hold_busy_agent"
    ASSIGN_IDLE_AGENT = "assign_idle_agent"
    COORDINATE_INTERNALLY = "coordinate_internally"
    REQUEST_INDEPENDENT_EXAMINATION = "request_independent_examination"
    RETURN_DEFECT_TO_BUILDER = "return_defect_to_builder"
    OBSERVE_OCCUPANCY = "observe_occupancy"
    REFUSE_AUTHORITY = "refuse_authority"
    LOOKUP_UNKNOWN = "lookup_unknown"


class OccupancyState(str, enum.Enum):
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class WorkspaceMutability(str, enum.Enum):
    MUTABLE_BUILDER = "mutable_builder"
    READ_ONLY_EXAMINER = "read_only_examiner"


class ArtifactRole(str, enum.Enum):
    FROZEN_CANDIDATE = "frozen_candidate"
    PARENT_SHA = "parent_sha"
    CURRENT_BRANCH_TIP = "current_branch_tip"
    EXAMINED_LANE_SHA = "examined_lane_sha"
    UNSPECIFIED = "unspecified"


INTERRUPT_KINDS = frozenset(
    {
        InterruptKind.FOUNDER_ONLY_AUTHORITY,
        InterruptKind.MATERIAL_PRODUCT_DECISION,
        InterruptKind.SECURITY_POLICY,
        InterruptKind.MONEY_BUDGET,
        InterruptKind.DESTRUCTIVE_ACTION,
        InterruptKind.UNRESOLVED_ALTERNATIVES,
        InterruptKind.GENUINE_BLOCKER,
    }
)


@dataclass(frozen=True)
class InboundClassification:
    kind: InboundKind
    relay_categories: tuple[RelayCategory, ...] = ()
    interrupt: InterruptKind = InterruptKind.NONE
    reason: str = ""
    language: str = "en"

    @property
    def interrupt_founder(self) -> bool:
        return self.interrupt in INTERRUPT_KINDS


@dataclass(frozen=True)
class OutboundDecision:
    disposition: OutboundDisposition
    original: str
    content: str
    blocked_categories: tuple[RelayCategory, ...] = ()
    reason: str = ""

    @property
    def asks_founder_to_relay(self) -> bool:
        return self.disposition is not OutboundDisposition.SEND and bool(self.blocked_categories)


@dataclass(frozen=True)
class InternalAction:
    kind: InternalActionKind
    detail: str
    agent_key: str | None = None


@dataclass(frozen=True)
class OccupancyObservation:
    agent_key: str
    state: OccupancyState
    assignment_id: UUID | None = None
    task_id: UUID | None = None
    execution_id: UUID | None = None
    observed_at: datetime | None = None
    source: str = "unavailable"
    authoritative: bool = False


@dataclass(frozen=True)
class OccupancySnapshot:
    observations: tuple[OccupancyObservation, ...] = ()
    source: str = "unavailable"
    authoritative: bool = False

    @property
    def running_agents(self) -> tuple[str, ...]:
        return tuple(
            item.agent_key
            for item in self.observations
            if item.state is OccupancyState.RUNNING and item.authoritative
        )

    @property
    def idle_agents(self) -> tuple[str, ...]:
        return tuple(
            item.agent_key
            for item in self.observations
            if item.state is OccupancyState.IDLE and item.authoritative
        )


@dataclass(frozen=True)
class SoftwareTruth:
    branch: str
    sha: str | None = None
    ci_summary: str | None = None
    source: str = "unavailable"
    detail: str = ""
    entity_key: str = "unspecified"
    repository: str = "d1n095/LifeAI"
    artifact_role: str = ArtifactRole.UNSPECIFIED.value
    state: str = "unspecified"

    @property
    def founder_answer(self) -> str | None:
        if not self.sha:
            if self.source == "unavailable" or self.entity_key != "unspecified":
                return (
                    f"UNKNOWN: I could not resolve {self.entity_key} from an authoritative "
                    f"source ({self.source}). I will not ask you to relay it."
                )
            return None
        ci = f" CI: {self.ci_summary}." if self.ci_summary else ""
        return (
            f"{self.entity_key} is `{self.sha}` "
            f"(repository={self.repository}; branch={self.branch}; "
            f"role={self.artifact_role}; state={self.state}; source={self.source}).{ci} "
            "I read this internally. Do not relay SHAs, branches, or CI to agents."
        )


@dataclass(frozen=True)
class BoundSubject:
    entity_key: str
    repository: str
    branch: str
    artifact_role: ArtifactRole
    state: str
    authoritative_source: str
    sha: str | None = None
    detail: str = ""

    def as_software_truth(self, *, ci_summary: str | None = None) -> SoftwareTruth:
        return SoftwareTruth(
            branch=self.branch,
            sha=self.sha,
            ci_summary=ci_summary,
            source=self.authoritative_source,
            detail=self.detail,
            entity_key=self.entity_key,
            repository=self.repository,
            artifact_role=self.artifact_role.value,
            state=self.state,
        )


@dataclass(frozen=True)
class ConversationTurnResult:
    inbound: InboundClassification
    outbound: OutboundDecision | None
    internal_actions: tuple[InternalAction, ...]
    interrupt_founder: bool
    founder_message: str | None
    notes: tuple[str, ...] = ()
    software_truth: SoftwareTruth | None = None
    occupancy: OccupancySnapshot | None = None
    gated: bool = False


@dataclass(frozen=True)
class ProvenancePointer:
    kind: str
    value: str
    message_id: UUID | None = None
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class ActiveDecision:
    topic: str
    statement: str
    decision_id: UUID
    message_id: UUID | None
    identifiers: dict[str, str] = field(default_factory=dict)
    superseded: bool = False
