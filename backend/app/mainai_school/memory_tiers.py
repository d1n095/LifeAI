"""HOT / WARM / COLD classification policy — lossless history preserved.

Summaries may accelerate retrieval but NEVER replace provenance.

This module is a **policy classifier only**. When durable `app.memory_tiers`
(Stage N / frontier) is on the integration tip, school MUST delegate persist/
demote/search there — do not grow a second tier store here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class MemoryTier(str, Enum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


@dataclass(frozen=True)
class TierAssignment:
    tier: MemoryTier
    reason: str
    replaces_provenance: bool = False  # always False


def classify_memory_tier(
    *,
    is_active_goal: bool = False,
    is_critical_correction: bool = False,
    observed_at: datetime | None = None,
    now: datetime | None = None,
    superseded: bool = False,
) -> TierAssignment:
    now = now or datetime.utcnow()
    if is_active_goal or is_critical_correction:
        return TierAssignment(MemoryTier.HOT, "active_or_critical")
    if superseded:
        # Historical but must remain retrievable
        return TierAssignment(MemoryTier.COLD, "superseded_historical_lossless")
    if observed_at is None:
        return TierAssignment(MemoryTier.WARM, "unknown_age_default_warm")
    age = now - observed_at
    if age <= timedelta(days=14):
        return TierAssignment(MemoryTier.HOT, "recent_14d")
    if age <= timedelta(days=180):
        return TierAssignment(MemoryTier.WARM, "recent_180d")
    return TierAssignment(MemoryTier.COLD, "older_than_180d_archive")


def tier_as_dict(t: TierAssignment) -> dict[str, Any]:
    return {
        "tier": t.tier.value,
        "reason": t.reason,
        "replaces_provenance": False,
        "canonical_history_lossless": True,
    }
