"""Authoritative agent occupancy for continuous conversation.

Caller-supplied busy_agents/idle_agents are not authority. Occupancy is observed
from agent/task/execution identity and distinguished as RUNNING, IDLE, STALE, or
UNKNOWN. This module does not grant assignments or security permissions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import RuntimeStatus, all_agents_runtime_snapshot
from app.mainai_continuous_conversation.types import OccupancyObservation, OccupancySnapshot, OccupancyState

logger = logging.getLogger(__name__)

FOUNDER_ALPHA_BUSY = ("claude",)
FOUNDER_ALPHA_IDLE = ("cursor", "codex")

_RUNNING = frozenset(
    {
        RuntimeStatus.RUNNING,
        RuntimeStatus.REVIEWING,
        RuntimeStatus.WAITING_REVIEW,
        RuntimeStatus.WAITING_DEPENDENCY,
        RuntimeStatus.BLOCKED,
    }
)
_STALE_AFTER = timedelta(hours=6)


def founder_alpha_occupancy() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Documented Founder Alpha occupancy *pattern*, not live authority."""

    return FOUNDER_ALPHA_BUSY, FOUNDER_ALPHA_IDLE


def founder_alpha_pattern_snapshot() -> OccupancySnapshot:
    now = datetime.now(timezone.utc)
    observations = (
        OccupancyObservation(agent_key="claude", state=OccupancyState.RUNNING, source="founder_alpha_pattern", authoritative=False, observed_at=now),
        OccupancyObservation(agent_key="cursor", state=OccupancyState.IDLE, source="founder_alpha_pattern", authoritative=False, observed_at=now),
        OccupancyObservation(agent_key="codex", state=OccupancyState.IDLE, source="founder_alpha_pattern", authoritative=False, observed_at=now),
    )
    return OccupancySnapshot(observations=observations, source="founder_alpha_pattern", authoritative=False)


def _state_for(view) -> OccupancyState:
    heartbeat = getattr(view, "updated_at", None) or getattr(view, "last_heartbeat_at", None)
    if heartbeat is not None:
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - heartbeat > _STALE_AFTER:
            return OccupancyState.STALE
    if view.runtime_status in _RUNNING:
        return OccupancyState.RUNNING
    if view.runtime_status is RuntimeStatus.IDLE:
        return OccupancyState.IDLE
    if view.runtime_status in {RuntimeStatus.COMPLETED, RuntimeStatus.FAILED, RuntimeStatus.OFFLINE}:
        return OccupancyState.IDLE
    return OccupancyState.UNKNOWN


def occupancy_snapshot(db: Session, *, owner_id: UUID) -> OccupancySnapshot:
    try:
        views = all_agents_runtime_snapshot(db, owner_id=owner_id)
    except Exception:
        logger.info("runtime occupancy snapshot unavailable", exc_info=True)
        return OccupancySnapshot(source="unavailable", authoritative=False)
    if not views:
        return OccupancySnapshot(source="runtime_snapshot_empty", authoritative=True)
    observations = []
    for view in views:
        observations.append(
            OccupancyObservation(
                agent_key=view.agent_key,
                state=_state_for(view),
                assignment_id=getattr(view, "assignment_id", None),
                task_id=getattr(view, "task_id", None),
                execution_id=getattr(view, "goal_id", None),
                observed_at=datetime.now(timezone.utc),
                source="runtime_snapshot",
                authoritative=True,
            )
        )
    return OccupancySnapshot(observations=tuple(observations), source="runtime_snapshot", authoritative=True)


def occupancy_for_turn(db: Session, *, owner_id: UUID) -> OccupancySnapshot:
    """Live occupancy. Empty/missing runtime state is UNKNOWN, not a hardcoded busy/idle list."""

    return occupancy_snapshot(db, owner_id=owner_id)


def caller_supplied_occupancy(*, busy_agents: tuple[str, ...] = (), idle_agents: tuple[str, ...] = ()) -> OccupancySnapshot:
    """Non-authoritative hint only. Must not be treated as task/session truth."""

    now = datetime.now(timezone.utc)
    observations = []
    for agent in busy_agents:
        observations.append(
            OccupancyObservation(agent_key=agent, state=OccupancyState.UNKNOWN, source="caller_supplied", authoritative=False, observed_at=now)
        )
    for agent in idle_agents:
        observations.append(
            OccupancyObservation(agent_key=agent, state=OccupancyState.UNKNOWN, source="caller_supplied", authoritative=False, observed_at=now)
        )
    return OccupancySnapshot(observations=tuple(observations), source="caller_supplied", authoritative=False)
