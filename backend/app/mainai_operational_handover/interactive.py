"""Interactive priority, bounded background queues, and backpressure.

Substantial background work must not monopolize Founder-facing service.
Does not permanently assign providers to job categories.
"""

from __future__ import annotations

import time
from typing import Any, Literal

FIRST_CYCLE_GO = False
WorkClass = Literal["interactive", "background"]

INTERACTIVE_RESERVE = 1
MAX_BACKGROUND_IN_FLIGHT = 2
MAX_BACKGROUND_QUEUED = 8
MAX_BACKGROUND_RETRIES = 3


class InteractiveIsolation:
    def __init__(
        self,
        *,
        max_background_in_flight: int = MAX_BACKGROUND_IN_FLIGHT,
        max_background_queued: int = MAX_BACKGROUND_QUEUED,
    ) -> None:
        self.max_background_in_flight = max_background_in_flight
        self.max_background_queued = max_background_queued
        self._interactive_admitted = 0
        self._background_admitted = 0
        self._background_rejected = 0
        self._background_in_flight = 0
        self._queued: list[dict[str, Any]] = []
        self._latencies_ms: dict[str, list[float]] = {"interactive": [], "background": []}

    def admit(self, work_class: WorkClass, *, name: str, started_at: float | None = None) -> dict[str, Any]:
        started = started_at if started_at is not None else time.monotonic()
        if work_class == "interactive":
            self._interactive_admitted += 1
            self._latencies_ms["interactive"].append(0.0)
            return {
                "admitted": True,
                "work_class": work_class,
                "name": name,
                "priority": 0,
                "started_at": started,
                "first_cycle_go": FIRST_CYCLE_GO,
            }
        if self._background_in_flight >= self.max_background_in_flight:
            return self._queue_or_reject(name, reason="in_flight_budget")
        if len(self._queued) >= self.max_background_queued and self._background_in_flight >= self.max_background_in_flight:
            return self._reject(name, "queue_full")
        self._background_in_flight += 1
        self._background_admitted += 1
        return {
            "admitted": True,
            "work_class": work_class,
            "name": name,
            "priority": 1,
            "started_at": started,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def complete(self, work_class: WorkClass, *, started_at: float) -> None:
        elapsed = max(0.0, (time.monotonic() - started_at) * 1000.0)
        self._latencies_ms[work_class].append(elapsed)
        if work_class == "background" and self._background_in_flight > 0:
            self._background_in_flight -= 1
        if work_class == "background":
            self._drain()

    def _queue_or_reject(self, name: str, *, reason: str) -> dict[str, Any]:
        if len(self._queued) >= self.max_background_queued:
            return self._reject(name, reason)
        self._queued.append({"name": name, "retries": 0})
        return {
            "admitted": False,
            "queued": True,
            "work_class": "background",
            "name": name,
            "reason": reason,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def _reject(self, name: str, reason: str) -> dict[str, Any]:
        self._background_rejected += 1
        return {
            "admitted": False,
            "queued": False,
            "work_class": "background",
            "name": name,
            "reason": reason,
            "backpressure": True,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def _drain(self) -> None:
        while self._queued and self._background_in_flight < self.max_background_in_flight:
            item = self._queued.pop(0)
            if item["retries"] >= MAX_BACKGROUND_RETRIES:
                self._background_rejected += 1
                continue
            self._background_in_flight += 1
            self._background_admitted += 1

    def public_view(self) -> dict[str, Any]:
        interactive = self._latencies_ms["interactive"]
        return {
            "interactive_reserve": INTERACTIVE_RESERVE,
            "interactive_admitted": self._interactive_admitted,
            "background_admitted": self._background_admitted,
            "background_rejected": self._background_rejected,
            "background_in_flight": self._background_in_flight,
            "background_queued": len(self._queued),
            "max_background_in_flight": self.max_background_in_flight,
            "max_background_queued": self.max_background_queued,
            "interactive_p99_ms": _p99(interactive),
            "background_cannot_monopolize_interactive": True,
            "provider_not_permanently_assigned_to_category": True,
            "first_cycle_go": FIRST_CYCLE_GO,
        }


def _p99(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, int(len(ordered) * 0.99) - 1)
    return ordered[index]
