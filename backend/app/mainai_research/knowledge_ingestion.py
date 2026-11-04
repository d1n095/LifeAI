"""File / Knowledge Ingestion Semantics -- heterogeneous historical material must never have
its epistemic status inflated on ingestion. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

A historical statement "I think X may be useful" must NOT become "Founder permanently decided
X." -- structurally enforced below: `KNOWLEDGE_ITEM_TRANSITIONS` has no direct edge from
MENTION/IDEA/ASSUMPTION to DECISION; reaching DECISION requires passing through CLAIM/PLAN
first, each an explicit, caller-driven transition, never an automatic promotion."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.mainai_research.types import KnowledgeItem, KnowledgeItemState, ResearchError

# Explicit transition table -- mirrors this codebase's own established `IDEA_DISPOSITION_
# TRANSITIONS` (`app.mainai_executive.idea_incubation`) and `LIFE_INTENT_TRANSITIONS`
# conventions: an explicit FROM/TO table, never an implicit "anything can become anything."
KNOWLEDGE_ITEM_TRANSITIONS: dict[KnowledgeItemState, frozenset[KnowledgeItemState]] = {
    KnowledgeItemState.UNKNOWN: frozenset({KnowledgeItemState.MENTION, KnowledgeItemState.IDEA, KnowledgeItemState.REJECTED}),
    KnowledgeItemState.MENTION: frozenset({KnowledgeItemState.IDEA, KnowledgeItemState.ASSUMPTION, KnowledgeItemState.CLAIM, KnowledgeItemState.REJECTED, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.IDEA: frozenset({KnowledgeItemState.ASSUMPTION, KnowledgeItemState.CLAIM, KnowledgeItemState.PLAN, KnowledgeItemState.REJECTED, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.ASSUMPTION: frozenset({KnowledgeItemState.CLAIM, KnowledgeItemState.REJECTED, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.CLAIM: frozenset({KnowledgeItemState.DECISION, KnowledgeItemState.PLAN, KnowledgeItemState.REJECTED, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.DECISION: frozenset({KnowledgeItemState.PLAN, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.PLAN: frozenset({KnowledgeItemState.IMPLEMENTATION, KnowledgeItemState.SUPERSEDED, KnowledgeItemState.REJECTED}),
    KnowledgeItemState.IMPLEMENTATION: frozenset({KnowledgeItemState.VERIFIED_RESULT, KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.VERIFIED_RESULT: frozenset({KnowledgeItemState.SUPERSEDED}),
    KnowledgeItemState.REJECTED: frozenset(),
    KnowledgeItemState.SUPERSEDED: frozenset(),
}

TERMINAL_STATES = frozenset({KnowledgeItemState.REJECTED, KnowledgeItemState.SUPERSEDED})


class KnowledgeIngestionError(ResearchError):
    pass


class InvalidKnowledgeTransitionError(KnowledgeIngestionError):
    pass


class TerminalKnowledgeStateError(InvalidKnowledgeTransitionError):
    pass


def ingest_knowledge_item(
    *, owner_id: str, what: str, source: str, original_vs_derived: str = "original",
    who: str | None = None, when: datetime | None = None, context: str | None = None,
    confidence: float | None = None, inference_vs_explicit: str = "explicit_text",
    initial_state: KnowledgeItemState = KnowledgeItemState.MENTION,
) -> KnowledgeItem:
    """Pure constructor -- never infers `initial_state` above MENTION/IDEA/ASSUMPTION from raw
    text alone; a caller asserting CLAIM/DECISION/PLAN/IMPLEMENTATION/VERIFIED_RESULT directly
    must do so explicitly and is expected to have real grounds (this function does not
    second-guess that, matching `record_founder_memory()`'s own "caller's own explicit
    assertion" doctrine), but it never happens as a side effect of merely reading a file."""

    if not what.strip():
        raise KnowledgeIngestionError("what must not be empty")
    if original_vs_derived not in ("original", "derived"):
        raise KnowledgeIngestionError("original_vs_derived must be 'original' or 'derived'")
    if inference_vs_explicit not in ("explicit_text", "inference"):
        raise KnowledgeIngestionError("inference_vs_explicit must be 'explicit_text' or 'inference'")
    return KnowledgeItem(
        item_id=str(uuid.uuid4()), owner_id=owner_id, what=what, who=who, when=when, context=context,
        source=source, original_vs_derived=original_vs_derived, state=initial_state, currentness="current",
        confidence=confidence, inference_vs_explicit=inference_vs_explicit,
    )


def transition_knowledge_item(item: KnowledgeItem, *, new_state: KnowledgeItemState) -> KnowledgeItem:
    """Fail-closed state machine -- "I think X may be useful" (MENTION/IDEA) has NO direct edge
    to DECISION; it must pass through CLAIM first (an explicit "this is asserted as true" step)
    before DECISION becomes reachable. Each call here is one explicit, caller-driven transition,
    never an automatic promotion triggered by re-reading the same source again."""

    if item.state in TERMINAL_STATES:
        raise TerminalKnowledgeStateError(f"knowledge item {item.item_id} is already terminal ({item.state.value})")
    if new_state not in KNOWLEDGE_ITEM_TRANSITIONS.get(item.state, frozenset()):
        raise InvalidKnowledgeTransitionError(f"cannot transition knowledge item from {item.state.value} to {new_state.value}")
    from dataclasses import replace

    # `supersedes_item_id` is set at CREATION time by a caller ingesting the NEW item that
    # replaces this one (mirroring `ProjectEntity.supersedes_entity_id`'s own "new row points
    # back at what it replaces" convention) -- transitioning THIS item's own state to
    # SUPERSEDED never touches that field on itself.
    return replace(item, state=new_state, currentness="superseded" if new_state == KnowledgeItemState.SUPERSEDED else item.currentness)
