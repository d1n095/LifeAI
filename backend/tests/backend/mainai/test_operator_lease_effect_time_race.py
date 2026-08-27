"""Two-worker consequential effect race at Operator write boundary.

Stale worker (expired lease and/or superseded generation/locked_by) must produce
ZERO filesystem mutation. Winner may write once.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.development_operator.service import OperatorAuthorizationError, write_file
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
