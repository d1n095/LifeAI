"""Reserved Capability / Upcoming Need. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

AGENT_RESERVED != AGENT_WASTED -- but reservation must have real evidence, and must not be
indefinite.

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReservationAssessment:
    should_remain_reserved: bool
    reason: str


def assess_reservation(
    *,
    upcoming_task_known: bool,
    upcoming_task_requires_this_agent: bool,
    evidence_for_upcoming_need: bool,
    reserved_since_minutes: float,
    max_reservation_minutes: float = 60.0,
) -> ReservationAssessment:
    """AGENT_RESERVED != AGENT_WASTED: an idle agent held back for a real, evidenced upcoming
    need is not waste. But reservation without evidence, or held past
    `max_reservation_minutes`, is exactly the waste this function must catch."""

    if reserved_since_minutes >= max_reservation_minutes:
        return ReservationAssessment(False, f"reserved for {reserved_since_minutes:.0f}min (>= {max_reservation_minutes:.0f}min cap) -- indefinite reservation is not allowed regardless of upcoming need")

    if not upcoming_task_known or not upcoming_task_requires_this_agent:
        return ReservationAssessment(False, "no known upcoming task specifically requires this agent")

    if not evidence_for_upcoming_need:
        return ReservationAssessment(False, "upcoming need is claimed but not evidenced -- reservation requires real evidence, not a hunch")

    return ReservationAssessment(True, f"real, evidenced upcoming task requires this specific agent; reserved {reserved_since_minutes:.0f}min of {max_reservation_minutes:.0f}min cap -- AGENT_RESERVED != AGENT_WASTED")
