"""Deterministic regression for the WAITING_PROVIDER backoff floor.

`test_waiting_provider_wake_release.py::test_waiting_provider_defer_parks_blocked_with_backoff_not_immediate_ready`
was intermittently failing on CI with `assert next_retry_at > utcnow()` comparing a stored
timestamp that had landed a few hundred ms *behind* wall-clock now. Root cause is production,
not test luck: `compute_waiting_provider_retry_at` drew its delay from `compute_backoff_seconds`,
which uses *full* jitter — a uniform draw in [0, ceiling]. For the first attempt ceiling is only
`WAITING_PROVIDER_BACKOFF_BASE_SECONDS` (30s), so the low tail of the draw can be ~0s, scheduling
a retry essentially *now*. That both breaks the park's documented "blocked, not immediate ready —
do not hot-loop while the provider is down" invariant and, under any DB round-trip latency between
the defer and the assertion, makes `next_retry_at <= now`.

The floor is **base-gated** — `max(delay, min(MIN, base))` — not absolute. That matters: the
`test_composed_autonomy_soak*` family sets `WAITING_PROVIDER_BACKOFF_BASE_SECONDS = 0.0` to mean
"no backoff — this park is immediately wakeable" and then asserts the very next worker tick wakes
it to `ready`. An *absolute* 5s floor would override that explicit zero base, leave the task
`blocked`, and (in the multi-worker soak) deadlock the suite. The last two tests pin both halves
of the contract so neither can silently regress. See
`app/mainai_execution/provider_wait_wake.py::WAITING_PROVIDER_BACKOFF_MIN_SECONDS`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.jobs.retry as retry_mod
from app.mainai_execution import provider_wait_wake as pww

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def jitter(monkeypatch):
    """Control the full-jitter draw inside compute_backoff_seconds deterministically.

    compute_backoff_seconds returns random.uniform(0, ceiling); by replacing random.uniform we
    choose exactly where in [0, ceiling] the draw lands, including the pathological low tail.
    """

    def _set(fraction: float):
        # fraction in [0, 1]: 0.0 = worst-case near-zero draw, 1.0 = top of the jitter window.
        def fake_uniform(low, high):  # noqa: ANN001 - mirrors random.uniform signature
            return low + (high - low) * fraction

        monkeypatch.setattr(retry_mod.random, "uniform", fake_uniform)

    return _set


def test_worst_case_zero_jitter_still_schedules_strictly_future_retry(jitter):
    """The exact invariant the flaky integration test probed, now deterministic.

    Even when the jitter draw is 0 (the low tail that caused the flake), the first retry must be
    the floor in the future — never at or behind the defer instant.
    """
    jitter(0.0)
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    assert retry_at == _FIXED_NOW + timedelta(seconds=pww.WAITING_PROVIDER_BACKOFF_MIN_SECONDS)
    assert retry_at > _FIXED_NOW


def test_floor_holds_for_every_non_exhausted_attempt(jitter):
    """No attempt below the exhaustion bound may schedule a near-now retry under zero jitter."""
    jitter(0.0)
    for prior in range(pww.WAITING_PROVIDER_MAX_BACKOFFS):
        retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=prior, now=_FIXED_NOW)
        assert retry_at is not None
        delay = (retry_at - _FIXED_NOW).total_seconds()
        assert delay >= pww.WAITING_PROVIDER_BACKOFF_MIN_SECONDS
        assert retry_at > _FIXED_NOW


def test_healthy_jitter_draw_is_not_suppressed_by_the_floor(jitter):
    """The floor only clamps the low tail; a draw already above it is preserved unchanged.

    At the top of the jitter window for attempt 0 the draw is the full base (30s), far above the
    5s floor, so the floor must not shrink it.
    """
    jitter(1.0)
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    expected = _FIXED_NOW + timedelta(seconds=pww.WAITING_PROVIDER_BACKOFF_BASE_SECONDS)
    assert retry_at == expected
    # And it is strictly greater than a floor-only schedule, proving jitter spread survives.
    assert retry_at > _FIXED_NOW + timedelta(seconds=pww.WAITING_PROVIDER_BACKOFF_MIN_SECONDS)


def test_floor_is_a_floor_not_a_replacement(jitter):
    """A mid-window draw between the floor and the ceiling passes through untouched."""
    jitter(0.5)  # attempt 0: 0.5 * 30s = 15s, above the 5s floor
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    assert retry_at == _FIXED_NOW + timedelta(seconds=15.0)


def test_exhaustion_returns_none_without_a_clock(jitter):
    """At/after the max-backoff bound the task stays blocked with no retry clock (unchanged)."""
    jitter(0.0)
    assert (
        pww.compute_waiting_provider_retry_at(
            prior_backoffs=pww.WAITING_PROVIDER_MAX_BACKOFFS, now=_FIXED_NOW
        )
        is None
    )
    assert (
        pww.compute_waiting_provider_retry_at(
            prior_backoffs=pww.WAITING_PROVIDER_MAX_BACKOFFS + 5, now=_FIXED_NOW
        )
        is None
    )


def test_explicit_zero_base_is_not_floored_so_the_park_wakes_now(jitter, monkeypatch):
    """base=0 is an explicit "no backoff — wake now" signal and must NOT be floored.

    This is the contract the whole `test_composed_autonomy_soak*` family depends on
    (`monkeypatch.setattr(wake_module, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)` then
    assert the next worker tick wakes the park to `ready`). An absolute floor would schedule
    `now + 5s`, leave the task blocked, and deadlock the multi-worker soak. The base-gated
    floor `min(MIN, base)` collapses to 0 here, so the retry is `now` and the park is due.
    """
    monkeypatch.setattr(pww, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)
    jitter(0.0)
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    assert retry_at == _FIXED_NOW  # due immediately — next_retry_at <= now
    # Even the top of the (zero-width) jitter window stays at now, every non-exhausted attempt.
    jitter(1.0)
    for prior in range(pww.WAITING_PROVIDER_MAX_BACKOFFS):
        assert (
            pww.compute_waiting_provider_retry_at(prior_backoffs=prior, now=_FIXED_NOW)
            == _FIXED_NOW
        )


def test_production_base_is_still_floored_to_the_full_minimum(jitter, monkeypatch):
    """With the production base (30s) the floor is the full 5s — the original flake stays fixed.

    Guards the other direction: the base-gating must not weaken the floor whenever the base is
    at or above the minimum.
    """
    monkeypatch.setattr(pww, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 30.0)
    jitter(0.0)
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    assert retry_at == _FIXED_NOW + timedelta(seconds=5.0)
    # A base between 0 and MIN floors only up to that base, never above it.
    monkeypatch.setattr(pww, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 2.0)
    jitter(0.0)
    retry_at = pww.compute_waiting_provider_retry_at(prior_backoffs=0, now=_FIXED_NOW)
    assert retry_at == _FIXED_NOW + timedelta(seconds=2.0)
