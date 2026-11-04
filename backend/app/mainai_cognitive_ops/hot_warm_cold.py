"""Hot / Warm / Cold information temperature. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

COLD != IRRELEVANT. HOT != MORE TRUE. Temperature is a storage/retrieval concern only -- these
functions never read or write a confidence/truth field, by construction (no such parameter
exists here)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.mainai_cognitive_ops.types import InformationTemperature

DEFAULT_HOT_WINDOW_HOURS = 24.0
DEFAULT_WARM_WINDOW_DAYS = 30.0


def classify_temperature(
    *, last_used_at: datetime, now: datetime, hot_window_hours: float = DEFAULT_HOT_WINDOW_HOURS, warm_window_days: float = DEFAULT_WARM_WINDOW_DAYS,
) -> InformationTemperature:
    age_hours = (now - last_used_at).total_seconds() / 3600.0
    if age_hours <= hot_window_hours:
        return InformationTemperature.HOT
    if age_hours <= warm_window_days * 24.0:
        return InformationTemperature.WARM
    return InformationTemperature.COLD


@dataclass(frozen=True)
class TierTransition:
    item_id: str
    from_temperature: InformationTemperature
    to_temperature: InformationTemperature
    provenance_preserved: bool


def transition_tier(*, item_id: str, from_temperature: InformationTemperature, to_temperature: InformationTemperature, provenance: tuple[str, ...]) -> TierTransition:
    """A tier move never drops provenance -- COLD != FORGOTTEN, so `provenance_preserved` is
    only True when the caller actually passed non-empty provenance through; an empty tuple is
    reported as NOT preserved rather than silently assumed fine."""

    return TierTransition(item_id=item_id, from_temperature=from_temperature, to_temperature=to_temperature, provenance_preserved=bool(provenance))
