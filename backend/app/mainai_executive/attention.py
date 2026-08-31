"""Attention / interruption management for executive work.

pause / resume / supersede / defer / parallelize / cancel / replan — planning-side only.
No forgotten interrupted goal. No authority widening.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AttentionAction(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    SUPERSEDE = "supersede"
    DEFER = "defer"
    PARALLELIZE = "parallelize"
    CANCEL = "cancel"
    REPLAN = "replan"


@dataclass(frozen=True)
class AttentionDecision:
    action: AttentionAction
    reason: str
    keep_previous_goal: bool
    new_request_horizon: str
    authorized: bool = False


def decide_attention(
    *,
    current_critical: bool,
    incoming_urgent: bool,
    founder_says_first: bool,
    founder_says_later: bool,
    conflicting: bool,
) -> AttentionDecision:
    """Deterministic attention policy — founder phrases win when explicit."""
    if founder_says_first:
        return AttentionDecision(
            action=AttentionAction.SUPERSEDE if current_critical else AttentionAction.REPLAN,
            reason="founder_explicit_do_this_first",
            keep_previous_goal=True,  # never forget — park previous
            new_request_horizon="NOW",
        )
    if founder_says_later:
        return AttentionDecision(
            action=AttentionAction.DEFER,
            reason="founder_explicit_later",
            keep_previous_goal=True,
            new_request_horizon="LATER",
        )
    if conflicting:
        return AttentionDecision(
            action=AttentionAction.REPLAN,
            reason="conflicting_requests_need_replan",
            keep_previous_goal=True,
            new_request_horizon="NEAR",
        )
    if incoming_urgent and current_critical:
        return AttentionDecision(
            action=AttentionAction.PARALLELIZE,
            reason="urgent_during_critical_parallelize_or_queue",
            keep_previous_goal=True,
            new_request_horizon="NEAR",
        )
    if incoming_urgent and not current_critical:
        return AttentionDecision(
            action=AttentionAction.PAUSE,
            reason="urgent_pauses_noncritical",
            keep_previous_goal=True,
            new_request_horizon="NOW",
        )
    return AttentionDecision(
        action=AttentionAction.RESUME if current_critical else AttentionAction.DEFER,
        reason="default_continue_or_defer_trivial",
        keep_previous_goal=True,
        new_request_horizon="OPTIONAL",
    )


def attention_as_dict(d: AttentionDecision) -> dict[str, Any]:
    return {
        "action": d.action.value,
        "reason": d.reason,
        "keep_previous_goal": d.keep_previous_goal,
        "new_request_horizon": d.new_request_horizon,
        "authorized": False,
    }
