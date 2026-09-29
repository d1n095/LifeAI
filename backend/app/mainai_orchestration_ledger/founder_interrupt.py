"""Founder interruption policy.

The founder talks to MainAI. MainAI manages the machines. Routine coordination —
agent messages, SHAs, branch names, test results, wait-graphs, next-agent — stays
internal. Interrupt only for founder-only authority or a genuine unresolvable blocker.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.mainai_orchestration_ledger.types import AuthorityRequired


class FounderAttentionKind(str, enum.Enum):
    INTERNAL_COORDINATION = "internal_coordination"
    SHA_OR_BRANCH_RELAY = "sha_or_branch_relay"
    TEST_RESULT_RELAY = "test_result_relay"
    WAIT_GRAPH = "wait_graph"
    NEXT_AGENT = "next_agent"
    AGENT_MESSAGE_RELAY = "agent_message_relay"
    FOUNDER_ONLY_AUTHORITY = "founder_only_authority"
    MATERIAL_PRODUCT_DECISION = "material_product_decision"
    SECURITY_POLICY_DECISION = "security_policy_decision"
    MONEY_BUDGET_APPROVAL = "money_budget_approval"
    DESTRUCTIVE_ACTION_APPROVAL = "destructive_action_approval"
    UNRESOLVED_COMPETING_ALTERNATIVES = "unresolved_competing_alternatives"
    GENUINE_BLOCKER_NO_AGENT_CAN_RESOLVE = "genuine_blocker_no_agent_can_resolve"


INTERRUPT_KINDS = frozenset(
    {
        FounderAttentionKind.FOUNDER_ONLY_AUTHORITY,
        FounderAttentionKind.MATERIAL_PRODUCT_DECISION,
        FounderAttentionKind.SECURITY_POLICY_DECISION,
        FounderAttentionKind.MONEY_BUDGET_APPROVAL,
        FounderAttentionKind.DESTRUCTIVE_ACTION_APPROVAL,
        FounderAttentionKind.UNRESOLVED_COMPETING_ALTERNATIVES,
        FounderAttentionKind.GENUINE_BLOCKER_NO_AGENT_CAN_RESOLVE,
    }
)

INTERNAL_KINDS = frozenset(
    {
        FounderAttentionKind.INTERNAL_COORDINATION,
        FounderAttentionKind.SHA_OR_BRANCH_RELAY,
        FounderAttentionKind.TEST_RESULT_RELAY,
        FounderAttentionKind.WAIT_GRAPH,
        FounderAttentionKind.NEXT_AGENT,
        FounderAttentionKind.AGENT_MESSAGE_RELAY,
    }
)


@dataclass(frozen=True)
class InterruptDecision:
    interrupt: bool
    kind: FounderAttentionKind
    reason: str


def classify_founder_attention(kind: FounderAttentionKind, *, detail: str = "") -> InterruptDecision:
    if kind in INTERNAL_KINDS:
        return InterruptDecision(
            False,
            kind,
            detail or "routine agent coordination stays internal — do not interrupt the founder",
        )
    if kind in INTERRUPT_KINDS:
        return InterruptDecision(True, kind, detail or f"founder interruption required: {kind.value}")
    return InterruptDecision(False, kind, "unknown attention kind defaults to internal")


def authority_requires_founder(authority: AuthorityRequired) -> bool:
    return authority is not AuthorityRequired.NONE


def sha_relay_is_internal() -> InterruptDecision:
    return classify_founder_attention(
        FounderAttentionKind.SHA_OR_BRANCH_RELAY,
        detail="exact SHA/branch/CI are discovered from GitHub, never asked of the founder",
    )
