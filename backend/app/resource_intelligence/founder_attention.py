"""Founder attention as a finite resource -- the one resource this whole package otherwise
never accounts for. See docs/mainai_v2/MAINAI_RESOURCE_INTELLIGENCE_ROUND2_ADDENDUM.md for the
architecture decision this module implements.

Every `ContextLifecycleAction` implicitly costs the founder SOME amount of attention: a
`CONTINUE_CURRENT_SESSION` a founder never has to look at costs zero; a `RESET_SESSION` or
`HANDOFF` a founder has to notice, understand, and (today) manually execute costs real
attention, independent of whatever dollar cost is also involved. Pure, ordinal, hand-picked
classification -- same convention as `scheduler._ACTION_SEVERITY`'s own documented weighted
table, not a measured constant (no historical data exists yet to fit one against).

FOUNDER ATTENTION != DOLLAR COST: a cheap action can still be attention-expensive (a HANDOFF
with near-zero token cost still requires a founder decision to actually swap agents), and an
expensive action can be attention-free (a COMPACT can cost real tokens yet still needs zero
founder involvement). This module never conflates the two -- `decision.py` weighs them as
separate signals, never blends them into one number."""

from __future__ import annotations

import enum

from app.resource_intelligence.types import ContextLifecycleAction


class FounderAttentionLevel(str, enum.Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Ordinal weight per level -- used only for comparison (`>=`), never displayed as a metric
# value in its own right (this is policy vocabulary, not an observation; matches
# `scheduler._ACTION_SEVERITY`'s own "documented, hand-picked" convention exactly).
_LEVEL_WEIGHT = {
    FounderAttentionLevel.NONE: 0,
    FounderAttentionLevel.LOW: 1,
    FounderAttentionLevel.MEDIUM: 2,
    FounderAttentionLevel.HIGH: 3,
}

# Every action classified -- deliberately exhaustive (a KeyError on an unclassified action is
# preferred over a silent default that could under-report a real interruption).
ACTION_FOUNDER_ATTENTION: dict[ContextLifecycleAction, FounderAttentionLevel] = {
    ContextLifecycleAction.CONTINUE_CURRENT_SESSION: FounderAttentionLevel.NONE,
    ContextLifecycleAction.COMPACT: FounderAttentionLevel.NONE,
    ContextLifecycleAction.CHECKPOINT: FounderAttentionLevel.LOW,
    ContextLifecycleAction.KEEP_CURRENT_AGENT: FounderAttentionLevel.LOW,
    ContextLifecycleAction.SPLIT_JOB: FounderAttentionLevel.MEDIUM,
    ContextLifecycleAction.MOVE_SUBTASK: FounderAttentionLevel.MEDIUM,
    ContextLifecycleAction.CHANGE_MODEL: FounderAttentionLevel.MEDIUM,
    ContextLifecycleAction.CHANGE_PROVIDER: FounderAttentionLevel.MEDIUM,
    ContextLifecycleAction.DEFER: FounderAttentionLevel.MEDIUM,
    ContextLifecycleAction.RESET_SESSION: FounderAttentionLevel.HIGH,
    ContextLifecycleAction.HANDOFF: FounderAttentionLevel.HIGH,
}


def founder_attention_level(action: ContextLifecycleAction) -> FounderAttentionLevel:
    """Raises `KeyError` on an unclassified action -- deliberate, see module docstring."""

    return ACTION_FOUNDER_ATTENTION[action]


def attention_escalation(*, previous: ContextLifecycleAction | None, candidate: ContextLifecycleAction) -> bool:
    """True when `candidate` costs strictly MORE founder attention than `previous` did. `None`
    `previous` (no prior recommendation to compare against -- e.g. the very first tick of a
    session) is treated as `NONE`, so any candidate at or above `LOW` counts as an escalation
    the first time it is ever recommended."""

    previous_level = founder_attention_level(previous) if previous is not None else FounderAttentionLevel.NONE
    return _LEVEL_WEIGHT[founder_attention_level(candidate)] > _LEVEL_WEIGHT[previous_level]
