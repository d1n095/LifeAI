"""MainAI Resource Intelligence Round 2 -- `app.resource_intelligence.founder_attention` --
pure, no-db: every `ContextLifecycleAction` is classified (no `KeyError`), the ordinal
comparison in `attention_escalation()` behaves correctly, and FOUNDER ATTENTION != DOLLAR COST
is at least structurally distinct (its own module, its own vocabulary)."""

from __future__ import annotations

from app.resource_intelligence.founder_attention import (
    ACTION_FOUNDER_ATTENTION,
    FounderAttentionLevel,
    attention_escalation,
    founder_attention_level,
)
from app.resource_intelligence.types import ContextLifecycleAction


def test_every_action_is_classified():
    for action in ContextLifecycleAction:
        level = founder_attention_level(action)
        assert isinstance(level, FounderAttentionLevel)
    assert set(ACTION_FOUNDER_ATTENTION.keys()) == set(ContextLifecycleAction)


def test_continue_and_compact_cost_no_attention():
    assert founder_attention_level(ContextLifecycleAction.CONTINUE_CURRENT_SESSION) == FounderAttentionLevel.NONE
    assert founder_attention_level(ContextLifecycleAction.COMPACT) == FounderAttentionLevel.NONE


def test_reset_and_handoff_are_high_attention():
    assert founder_attention_level(ContextLifecycleAction.RESET_SESSION) == FounderAttentionLevel.HIGH
    assert founder_attention_level(ContextLifecycleAction.HANDOFF) == FounderAttentionLevel.HIGH


def test_escalation_from_none_to_high_is_true():
    assert attention_escalation(previous=ContextLifecycleAction.CONTINUE_CURRENT_SESSION, candidate=ContextLifecycleAction.HANDOFF) is True


def test_escalation_from_high_to_low_is_false():
    assert attention_escalation(previous=ContextLifecycleAction.HANDOFF, candidate=ContextLifecycleAction.CHECKPOINT) is False


def test_no_previous_action_treated_as_none():
    assert attention_escalation(previous=None, candidate=ContextLifecycleAction.CHECKPOINT) is True
    assert attention_escalation(previous=None, candidate=ContextLifecycleAction.CONTINUE_CURRENT_SESSION) is False


def test_same_level_is_not_an_escalation():
    assert attention_escalation(previous=ContextLifecycleAction.RESET_SESSION, candidate=ContextLifecycleAction.HANDOFF) is False
