"""Two-worker consequential effect race at Operator write boundary.

Stale worker (expired lease and/or superseded generation/locked_by) must produce
ZERO filesystem mutation. Winner may write once.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.development_driver import service as driver_svc
from app.development_driver.service import DriverStep, run_driver
from app.development_operator.service import LOCAL_WRITE, OperatorAuthorizationError, write_file
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAITaskWorktree
from tests.backend.mainai.test_autonomous_development_driver import _driver_foundation, _plan
from tests.backend.mainai.test_development_operator import _foundation


def test_expired_job_lease_blocks_write_with_zero_filesystem_effect(superuser_db, tmp_path):
    _, _, _, job, _, context = _foundation(superuser_db, tmp_path)
    target = context.repository_root / "safe.txt"
    original = target.read_text(encoding="utf-8")
    before = hashlib.sha256(original.encode()).hexdigest()

    job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.flush()

    with pytest.raises(OperatorAuthorizationError, match="stale or absent"):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content="stale-expired\n",
            expected_sha256=before,
            idempotency_key="expired-lease-write",
        )
    assert target.read_text(encoding="utf-8") == original


def test_takeover_generation_bump_blocks_stale_worker_write_zero_fs_effect(
    superuser_db, tmp_path
):
    _, _, _, job, worktree, context = _foundation(superuser_db, tmp_path)
    target = context.repository_root / "safe.txt"
    original = target.read_text(encoding="utf-8")
    before = hashlib.sha256(original.encode()).hexdigest()

    # Simulate takeover: new worker claims job with bumped generation.
    job.locked_by = "worker-2"
    job.lease_generation = context.lease_generation + 1
    job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    worktree.lease_generation = job.lease_generation
    superuser_db.flush()

    with pytest.raises(OperatorAuthorizationError, match="stale or absent"):
        write_file(
            superuser_db,
            context,  # still bound to worker-1 / old generation
            path="safe.txt",
            content="stale-takeover\n",
            expected_sha256=before,
            idempotency_key="stale-takeover-write",
        )
    assert target.read_text(encoding="utf-8") == original

    # Winner context can write once.
    winner = replace(
        context,
        worker_id="worker-2",
        lease_generation=job.lease_generation,
    )
    # Update marker for new generation ownership.
    import json

    marker = {
        "task_id": str(context.task_id),
        "job_id": str(context.job_id),
        "marker_token": worktree.marker_token,
    }
    (context.repository_root / ".mainai_worktree_owner.json").write_text(json.dumps(marker))
    ok = write_file(
        superuser_db,
        winner,
        path="safe.txt",
        content="winner\n",
        expected_sha256=before,
        idempotency_key="winner-write",
    )
    assert ok.result == "succeeded"
    assert target.read_text(encoding="utf-8") == "winner\n"


def test_genuine_cross_session_takeover_mid_run_blocks_next_step_zero_effect(
    superuser_db, tmp_path
):
    """V1 Stage 2 prep: unlike the sequential simulation above (same-session mutation before
    the call), this is a GENUINE two-connection race -- worker A's Driver run completes step
    1, then a truly separate session/connection commits a real takeover (new worker_id,
    bumped lease_generation) BETWEEN steps, and worker A's own next step attempt (still using
    its now-stale in-memory OperatorContext) must observe it.

    SAFETY holds: confirmed zero filesystem effect from step 2, via run_driver()'s own
    between-steps db.refresh(job) (development_driver/service.py:533) picking up the
    genuinely separate session's committed change under READ COMMITTED -- this is NOT the
    same-session identity-map staleness #199 fixed, it's proof the refresh mechanism also
    correctly observes a truly external commit, not just a same-session mutation.

    FINDING, not a bug, recorded precisely: run_driver()'s own per-step try/except
    (development_driver/service.py:551-553) only catches OperatorCapabilityMissing
    gracefully -- OperatorAuthorizationError from a mid-run stale-lease/takeover detection is
    NOT caught, so it propagates uncaught out of run_driver() instead of becoming a clean
    DriverResult (e.g. a STALE_AUTHORITY classification with its own checkpoint) the way
    cancellation (job.cancel_requested check, same loop) and capability-missing both do.
    Worker's own per-goal try/except (_advance_authorized_supervisor_goals) still catches
    this at the tick level and rolls back cleanly -- no crash of the wider poll loop -- but
    the superseded task's OWN audit trail never records a clean "stopped: superseded by
    takeover" checkpoint the way a cancel does. Observability/audit gap, not an authority
    gap; worth adding OperatorAuthorizationError to this except clause for parity."""
    _, _, task, job, worktree, context = _driver_foundation(superuser_db, tmp_path)
    context = replace(context, allowed_paths=("a.txt", "b.txt"))
    job_id, worktree_id = job.id, worktree.id
    original_lease_generation = job.lease_generation

    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)
    real_invoke = driver_svc._invoke_operator

    def _invoke_then_real_takeover(db, ctx, step, idem):
        result = real_invoke(db, ctx, step, idem)
        # Genuine takeover via a SEPARATE session/connection, not a mutation on the
        # Driver's own `db` session.
        takeover_session = Session()
        try:
            job_row = takeover_session.get(MainAIJob, job_id)
            wt_row = takeover_session.get(MainAITaskWorktree, worktree_id)
            job_row.locked_by = "worker-genuine-takeover"
            job_row.lease_generation = original_lease_generation + 1
            wt_row.lease_generation = original_lease_generation + 1
            takeover_session.commit()
        finally:
            takeover_session.close()
        return result

    superuser_db.commit()  # step 1's real effect visible to the takeover session
    driver_svc._invoke_operator = _invoke_then_real_takeover
    try:
        plan = _plan(
            context,
            "genuine-takeover-mid-run",
            DriverStep(
                "create_file", "first authorized write", "a.txt exists",
                {"path": "a.txt", "content": "first\n", "expected_sha256": None}, LOCAL_WRITE,
            ),
            DriverStep(
                "create_file", "second write must not run after real takeover",
                "b.txt must not exist",
                {"path": "b.txt", "content": "second\n", "expected_sha256": None}, LOCAL_WRITE,
            ),
        )
        with pytest.raises(OperatorAuthorizationError, match="stale or absent"):
            run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    finally:
        driver_svc._invoke_operator = real_invoke

    assert (context.repository_root / "a.txt").read_text(encoding="utf-8") == "first\n"
    assert not (context.repository_root / "b.txt").exists(), (
        "second write must NOT land -- the genuinely separate takeover session's commit "
        "must be observed by run_driver()'s own between-steps db.refresh(job)"
    )
