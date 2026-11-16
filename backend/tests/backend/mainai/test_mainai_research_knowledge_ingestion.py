"""MainAI Research -- `app.mainai_research.knowledge_ingestion` -- proves a historical idea
never silently becomes a permanent founder decision: "I think X may be useful" (MENTION/IDEA)
has no direct transition edge to DECISION.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import pytest

from app.mainai_research.knowledge_ingestion import (
    InvalidKnowledgeTransitionError,
    TerminalKnowledgeStateError,
    ingest_knowledge_item,
    transition_knowledge_item,
)
from app.mainai_research.types import KnowledgeItemState


def test_idea_cannot_directly_become_a_decision():
    item = ingest_knowledge_item(owner_id="o1", what="I think X may be useful", source="chat_log", initial_state=KnowledgeItemState.IDEA)
    with pytest.raises(InvalidKnowledgeTransitionError):
        transition_knowledge_item(item, new_state=KnowledgeItemState.DECISION)


def test_idea_must_pass_through_claim_before_decision():
    item = ingest_knowledge_item(owner_id="o1", what="I think X may be useful", source="chat_log", initial_state=KnowledgeItemState.IDEA)
    claimed = transition_knowledge_item(item, new_state=KnowledgeItemState.CLAIM)
    decided = transition_knowledge_item(claimed, new_state=KnowledgeItemState.DECISION)
    assert decided.state == KnowledgeItemState.DECISION


def test_mention_cannot_directly_become_implementation():
    item = ingest_knowledge_item(owner_id="o1", what="mentioned in passing", source="email", initial_state=KnowledgeItemState.MENTION)
    with pytest.raises(InvalidKnowledgeTransitionError):
        transition_knowledge_item(item, new_state=KnowledgeItemState.IMPLEMENTATION)


def test_terminal_state_cannot_transition_further():
    item = ingest_knowledge_item(owner_id="o1", what="x", source="s", initial_state=KnowledgeItemState.MENTION)
    rejected = transition_knowledge_item(item, new_state=KnowledgeItemState.REJECTED)
    with pytest.raises(TerminalKnowledgeStateError):
        transition_knowledge_item(rejected, new_state=KnowledgeItemState.CLAIM)


def test_ingestion_never_infers_state_above_the_explicit_caller_assertion():
    item = ingest_knowledge_item(owner_id="o1", what="we decided to use Postgres", source="meeting_notes")
    assert item.state == KnowledgeItemState.MENTION  # default -- never auto-promoted to DECISION from the text alone


def test_empty_what_is_rejected():
    from app.mainai_research.knowledge_ingestion import KnowledgeIngestionError

    with pytest.raises(KnowledgeIngestionError):
        ingest_knowledge_item(owner_id="o1", what="", source="s")
