"""Keep large-file work off the interactive Founder path."""

from __future__ import annotations

import threading
import time
from typing import Callable


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

    def release_background(self) -> None:
        if self.in_flight:
            self.in_flight -= 1
            if self.queued:
                self.queued -= 1
                self.in_flight += 1


class MeasuredBackgroundIsolation(BackgroundIsolation):
    """Run one background job while timing interactive admits.

    Measurements prove isolation on this process. They do not certify 5 GB.
    """

    def measure_interactive_during(
        self,
        background: Callable[[], None],
        interactive: Callable[[], None],
        *,
        interactive_calls: int = 20,
    ) -> dict[str, object]:
        admitted = self.admit("background", name="measured-import")
        if not admitted.get("admitted"):
            return {"outcome": "backpressure", **admitted, "five_gb_supported": False}
        samples: list[float] = []
        error: BaseException | None = None

        def _run() -> None:
            nonlocal error
            try:
                background()
            except BaseException as exc:  # noqa: BLE001 — surface to measurement
                error = exc
            finally:
                self.release_background()

        worker = threading.Thread(target=_run, name="large-file-background", daemon=True)
        worker.start()
        for index in range(interactive_calls):
            gate = self.admit("interactive", name=f"founder-{index}")
            if gate.get("admitted") is not True:
                raise RuntimeError("interactive work was blocked by background import")
            started = time.perf_counter()
            interactive()
            samples.append((time.perf_counter() - started) * 1000.0)
            if not worker.is_alive() and index + 1 >= min(8, interactive_calls):
                break
        worker.join(timeout=30)
        if error is not None:
            raise error
        return {
            "outcome": "measured",
            "interactive_admitted": self.interactive_admitted,
            "interactive_samples": len(samples),
            "interactive_max_ms": max(samples) if samples else 0.0,
            "interactive_p95_ms": sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else 0.0,
            "background_blocked_interactive": False,
            "five_gb_supported": False,
            "five_by_five_gb_measured": False,
        }
