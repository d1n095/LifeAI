"""Bounded operational metrics for the consultation surface (P5.5/P5.6).

Design constraints:
* FIXED key set — the snapshot is always the same small dict of integers; no dynamic
  labels, so no owner ids, prompts, answers, models or error text can ever leak into it.
* Process-lifetime singleton (like the workforce kill-switch state) with a
  reset_for_tests helper; counters are ints guarded by a lock.
* Bounded by construction: counters grow to at most their int value, never accumulate
  per-request objects.
"""

from __future__ import annotations

import threading
import time


class ConsultationMetrics:
    """Counters + coarse latency for ONE process. `snapshot()` is the only read surface."""

    KEYS = ("admitted", "refused", "provider_errors")
    MAX_LATENCY_MS_CEILING = 3_600_000  # clamp: one hour — keeps ints bounded

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._admitted = 0
        self._refused = 0
        self._provider_errors = 0
        self._last_latency_ms = 0
        self._max_latency_ms = 0

    def record_admitted(self, latency_ms: int) -> None:
        latency = max(0, min(int(latency_ms), self.MAX_LATENCY_MS_CEILING))
        with self._lock:
            self._admitted += 1
            self._last_latency_ms = latency
            self._max_latency_ms = max(self._max_latency_ms, latency)

    def record_refused(self) -> None:
        with self._lock:
            self._refused += 1

    def record_provider_error(self) -> None:
        with self._lock:
            self._provider_errors += 1

    def snapshot(self) -> dict:
        """Fixed-key integer snapshot — safe for /metrics surfaces because there is no
        field that could carry conversation content or owner identity."""
        with self._lock:
            return {
                "admitted": self._admitted,
                "refused": self._refused,
                "provider_errors": self._provider_errors,
                "last_latency_ms": self._last_latency_ms,
                "max_latency_ms": self._max_latency_ms,
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._admitted = 0
            self._refused = 0
            self._provider_errors = 0
            self._last_latency_ms = 0
            self._max_latency_ms = 0


consultation_metrics = ConsultationMetrics()


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
