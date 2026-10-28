"""Priority 2 — Real runtime continuity.

Independently determine whether a PERSISTENT scheduler/worker advances the operating cycle
without someone manually calling run_authorized_operating_cycle.

Findings this suite establishes executably:
  * app.worker.Worker IS a genuine restart-safe runtime loop (startup, idle poll, failure
    isolation, graceful shutdown) — but it only drains mainai_jobs (execution pipeline).
  * app.scheduler.start_scheduler starts ONLY the token-retention cleanup job.
  * run_authorized_operating_cycle (the #314 autonomous operating cycle) has NO production
    caller/driver — it is a manually invoked tick. RT3 records that missing runtime boundary
    as an executable FAIL/BLOCKED requirement (strict xfail -> flips when a driver is wired).
  * The product itself reports unattended_operating_cycle_ready == False (not hidden).

Target: #314 head 2308b697. first_cycle_go=false. No paid calls, no production mutation.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _guards import _no_egress  # noqa: F401


# ---------------------------------------------------------------------------
# RT1 — Worker is a real persistent loop with graceful shutdown (startup -> poll -> shutdown)
# ---------------------------------------------------------------------------
def test_RT1_worker_loop_starts_polls_and_shuts_down(monkeypatch):
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod, "record_worker_heartbeat", lambda *a, **k: None)
    w = worker_mod.Worker()
    polls = {"n": 0}

    async def fake_run_once():
        polls["n"] += 1
        return False   # nothing claimable -> loop will wait on the shutdown event

    monkeypatch.setattr(w, "run_once", fake_run_once)

    async def drive():
        task = asyncio.ensure_future(w.run())
        await asyncio.sleep(0.15)      # allow at least one poll
        assert not task.done()         # the loop is genuinely running/waiting, not exited early
        w.request_shutdown()           # SIGTERM/SIGINT path sets this same event
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(drive())
    assert polls["n"] >= 1             # it actually polled before shutting down


# ---------------------------------------------------------------------------
# RT2 — a poll-loop-level bug never kills the worker process (failure isolation + recovery)
# ---------------------------------------------------------------------------
def test_RT2_worker_loop_survives_run_once_exception(monkeypatch):
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod, "record_worker_heartbeat", lambda *a, **k: None)
    w = worker_mod.Worker()
    calls = {"n": 0}

    async def flaky_run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient poll-loop bug")
        w.request_shutdown()           # second iteration: ask the loop to stop
        return False

    monkeypatch.setattr(w, "run_once", flaky_run_once)
    asyncio.run(asyncio.wait_for(w.run(), timeout=10))     # must NOT propagate the RuntimeError
    assert calls["n"] >= 2             # recovered from the exception and kept polling


# ---------------------------------------------------------------------------
# RT3 — MISSING RUNTIME BOUNDARY (executable FAIL/BLOCKED): no production driver advances
#        the operating cycle. strict xfail -> flips to a red XPASS when a driver is wired.
# ---------------------------------------------------------------------------
def _app_py_files() -> list[Path]:
    import app

    root = Path(app.__file__).resolve().parent
    return [p for p in root.rglob("*.py")]


def _production_callers_of(symbol: str) -> list[str]:
    """Files under app/ that CALL `symbol(` (excluding the module that defines it)."""
    import app.agent_coordination.autonomous_cycle as ac

    definer = Path(ac.__file__).resolve()
    hits = []
    for p in _app_py_files():
        if p.resolve() == definer:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if f"{symbol}(" in text:
            hits.append(str(p))
    return hits


@pytest.mark.xfail(strict=True, reason="RT3/RUNTIME-BOUNDARY: run_authorized_operating_cycle has "
                   "NO production caller/driver — no scheduler job and no worker handler invokes it, "
                   "so the autonomous operating cycle only advances when something manually calls the "
                   "tick. This is a manual tick, not autonomous production operation.")
def test_RT3_operating_cycle_has_a_production_driver():
    callers = _production_callers_of("run_authorized_operating_cycle")
    assert callers, (
        "no production code under app/ drives run_authorized_operating_cycle; "
        "the autonomous operating cycle is a manually invoked tick only"
    )


# ---------------------------------------------------------------------------
# RT3-evidence (PASS): pin the current reality — zero production drivers today
# ---------------------------------------------------------------------------
def test_RT3evidence_zero_production_drivers_today():
    assert _production_callers_of("run_authorized_operating_cycle") == []


# ---------------------------------------------------------------------------
# RT4 — the production in-process scheduler runs ONLY token cleanup (no operating cycle, no worker)
# ---------------------------------------------------------------------------
def test_RT4_scheduler_runs_only_token_cleanup(monkeypatch):
    import app.scheduler as sched

    monkeypatch.setattr(sched, "run_token_cleanup", lambda db: None)      # no DB side effects
    monkeypatch.setattr(sched.settings, "enable_scheduled_cleanup", True, raising=False)
    monkeypatch.setattr(sched.settings, "cleanup_interval_hours", 24, raising=False)

    sched.stop_scheduler()
    sched.start_scheduler()
    try:
        ids = {job.id for job in sched._scheduler.get_jobs()}
        assert ids == {"token_cleanup"}, ids    # nothing advances the operating cycle here
    finally:
        sched.stop_scheduler()


# ---------------------------------------------------------------------------
# RT5 — the product itself honestly reports the unattended operating cycle is NOT ready
# ---------------------------------------------------------------------------
def test_RT5_product_reports_unattended_cycle_not_ready():
    from app.agent_coordination.connect import operational_handover_view
    from app.agent_coordination.durable import DurableCoordinationService

    view = operational_handover_view(DurableCoordinationService())
    assert view["unattended_operating_cycle_ready"] is False
