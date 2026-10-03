"""Continuous founder↔MainAI conversation vocabulary.

The founder talks to MainAI. MainAI manages machines. Conversation text never grants
merge, deploy, Recall, provider, or RLS authority.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


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
class SoftwareTruth:
    branch: str
    sha: str | None = None
    ci_summary: str | None = None
    source: str = "unavailable"
    detail: str = ""

    @property
    def founder_answer(self) -> str | None:
        if not self.sha:
            return None
        ci = f" CI: {self.ci_summary}." if self.ci_summary else ""
        return (
            f"GitHub reports `{self.branch}` at `{self.sha}`.{ci} "
            "I read this internally. Do not paste SHAs, branches, or CI to agents."
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
