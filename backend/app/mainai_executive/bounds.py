"""Bounded generation for the executive look-around scan.

Mirrors GapGenerationBounds shape (docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §2)
without importing autonomous_gap — this fires BEFORE execution, not after a verification miss.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutiveScanBounds:
    max_candidates_per_scan: int = 10
    max_scan_depth: int = 2
    max_elapsed_seconds: int = 60
    max_horizon_items: int = 48  # long-horizon plan slots (PLAN != AUTHORITY)

    def __post_init__(self) -> None:
        if not (1 <= self.max_candidates_per_scan <= 50):
            raise ValueError("max_candidates_per_scan out of bounds")
        if not (0 <= self.max_scan_depth <= 10):
            raise ValueError("max_scan_depth out of bounds")
        if not (1 <= self.max_elapsed_seconds <= 300):
            raise ValueError("max_elapsed_seconds out of bounds")
        if not (1 <= self.max_horizon_items <= 500):
            raise ValueError("max_horizon_items out of bounds")
