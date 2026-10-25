"""Keep large-file work off the interactive Founder path."""

from __future__ import annotations


class BackgroundIsolation:
    def __init__(self, *, max_in_flight: int = 1, max_queued: int = 2) -> None:
        self.max_in_flight = max_in_flight
        self.max_queued = max_queued
        self.in_flight = 0
        self.queued = 0
        self.interactive_admitted = 0

    def admit(self, work_class: str, *, name: str) -> dict[str, object]:
        if work_class == "interactive":
            self.interactive_admitted += 1
            return {"admitted": True, "name": name, "work_class": work_class}
        if self.in_flight >= self.max_in_flight:
            if self.queued >= self.max_queued:
                return {"admitted": False, "backpressure": True, "name": name}
            self.queued += 1
            return {"admitted": False, "queued": True, "name": name}
        self.in_flight += 1
        return {"admitted": True, "name": name, "work_class": work_class}
