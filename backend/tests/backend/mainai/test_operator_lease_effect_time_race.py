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
from app.development_operator.service import (
    LOCAL_WRITE,
    OperatorAuthorityTransitionError,
    write_file,
)
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

    # Lease expiry is an expected authority transition, not a programming defect --
    # must raise the narrower OperatorAuthorityTransitionError subclass specifically.
    with pytest.raises(OperatorAuthorityTransitionError, match="stale or absent"):
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

    # Takeover is an expected authority transition -- must raise the narrower
    # OperatorAuthorityTransitionError subclass specifically, not just the base class.
    with pytest.raises(OperatorAuthorityTransitionError, match="stale or absent"):
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

    CLOSED (was a real observability gap, now fixed): run_driver()'s per-step try/except
    now also catches OperatorAuthorityTransitionError -- a narrower OperatorAuthorizationError
    subclass reserved for expected authority transitions (lease takeover/expiry, founder
    cancel, envelope revoke, concurrent worktree/branch advance) -- and returns a clean
    DriverResult with classification="STALE_AUTHORITY" plus its own durable checkpoint,
    mirroring the existing cancel-path handling in the same loop. A founder reading the audit
    trail can now tell "superseded by another worker" apart from "founder cancelled". Prior
    behavior (still asserted by this test's predecessor in git history) was an uncaught
    exception propagating out of run_driver() -- Worker's own per-goal try/except still
    caught it at the tick level with no crash of the wider poll loop, but the superseded
    task's own audit trail never recorded a clean disposition."""
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
        result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    finally:
        driver_svc._invoke_operator = real_invoke

    assert result.phase == "BLOCKED"
    assert result.classification == "STALE_AUTHORITY"
    assert result.completed_steps == 1
    assert "stale or absent" in result.detail["reason"]
    assert (context.repository_root / "a.txt").read_text(encoding="utf-8") == "first\n"
    assert not (context.repository_root / "b.txt").exists(), (
        "second write must NOT land -- the genuinely separate takeover session's commit "
        "must be observed by run_driver()'s own between-steps db.refresh(job)"
    )

    # The checkpoint itself is durable and carries the classification/reason.
    from app.mainai_execution.checkpoint import latest_checkpoint_for_step

    checkpoint = latest_checkpoint_for_step(
        superuser_db, task_id=task.id, job_id=job_id, step="development_driver"
    )
    assert checkpoint is not None
    assert checkpoint.executor_state["classification"] == "STALE_AUTHORITY"
    assert checkpoint.executor_state["phase"] == "BLOCKED"


def test_winning_worker_resumes_from_stale_authority_checkpoint_and_completes(
    superuser_db, tmp_path
):
    """V1 Stage 2: closes the 'recovery after takeover' gap this session's own prep doc
    flagged as untested (docs/MAINAI_V1_STAGE2_STAGE3_ADVERSARIAL_PREP.md: 'does the WINNING
    worker's own subsequent tick correctly pick up and continue the task -- not yet
    empirically tested'). Composes the STALE_AUTHORITY fix with run_driver()'s own existing
    checkpoint-resume design: the LOSING worker's run above must leave state["next_step"]
    UNADVANCED past the refused step (proven here, not assumed) so the WINNING worker's own
    later run_driver() call -- same task/job/plan, new worker_id + bumped lease_generation --
    resumes from that exact step and completes it for real, with zero duplication of the
    already-completed first step."""
    _, _, task, job, worktree, context = _driver_foundation(superuser_db, tmp_path)
    context = replace(context, allowed_paths=("a.txt", "b.txt"))
    task.verification_plan = []
    job_id, worktree_id = job.id, worktree.id
    original_lease_generation = job.lease_generation

    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)
    real_invoke = driver_svc._invoke_operator

    def _invoke_then_real_takeover(db, ctx, step, idem):
        result = real_invoke(db, ctx, step, idem)
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

    superuser_db.commit()
    driver_svc._invoke_operator = _invoke_then_real_takeover
    plan = _plan(
        context,
        "genuine-takeover-then-resume",
        DriverStep(
            "create_file", "first authorized write", "a.txt exists",
            {"path": "a.txt", "content": "first\n", "expected_sha256": None}, LOCAL_WRITE,
        ),
        DriverStep(
            "create_file", "second write, must resume after takeover", "b.txt exists",
            {"path": "b.txt", "content": "second\n", "expected_sha256": None}, LOCAL_WRITE,
        ),
    )
    try:
        losing_result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    finally:
        driver_svc._invoke_operator = real_invoke

    assert losing_result.classification == "STALE_AUTHORITY"
    assert losing_result.completed_steps == 1
    assert not (context.repository_root / "b.txt").exists()

    # The WINNING worker: same task/job/plan, its own real worker_id + the generation the
    # takeover session actually committed -- exactly what a second, genuinely separate
    # worker process resuming this goal would construct for itself.
    winner_context = replace(
        context,
        worker_id="worker-genuine-takeover",
        lease_generation=original_lease_generation + 1,
    )
    winning_result = run_driver(
        superuser_db, context=winner_context, plan=plan, max_actions=10
    )

    assert winning_result.classification == "COMPLETE"
    # Zero duplication: exactly the 2 real steps, not 3 (no re-run of the already-completed
    # first step just because a second run_driver() call happened).
    assert winning_result.completed_steps == 2
    assert (context.repository_root / "a.txt").read_text(encoding="utf-8") == "first\n"
    assert (context.repository_root / "b.txt").read_text(encoding="utf-8") == "second\n"
