"""Live agent occupancy for continuous conversation.

Busy agents are held. Idle agents get independent lanes. This module only
observes runtime state — it does not grant assignments or security permissions.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import RuntimeStatus, all_agents_runtime_snapshot

logger = logging.getLogger(__name__)

FOUNDER_ALPHA_BUSY = ("claude",)
FOUNDER_ALPHA_IDLE = ("cursor", "codex")
_BUSY = frozenset(
    {
        RuntimeStatus.RUNNING,
        RuntimeStatus.REVIEWING,
        RuntimeStatus.WAITING_REVIEW,
        RuntimeStatus.WAITING_DEPENDENCY,
        RuntimeStatus.BLOCKED,
    }
)


def founder_alpha_occupancy() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return FOUNDER_ALPHA_BUSY, FOUNDER_ALPHA_IDLE


def occupancy_for_turn(db: Session, *, owner_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        views = all_agents_runtime_snapshot(db, owner_id=owner_id)
    except Exception:
        logger.info("runtime occupancy snapshot unavailable; using Founder Alpha occupancy", exc_info=True)
        return founder_alpha_occupancy()
    if not views:
        return founder_alpha_occupancy()
    busy = tuple(view.agent_key for view in views if view.runtime_status in _BUSY)
    idle = tuple(view.agent_key for view in views if view.runtime_status is RuntimeStatus.IDLE)
    return busy, idle
