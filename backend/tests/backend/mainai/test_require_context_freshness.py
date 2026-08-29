"""Final Operator fence (_require_context) must own job/task freshness.

Session A may hold a stale identity-mapped MainAIJob/MainAITask. Session B can
commit cancel or lease takeover. Session A's next Operator effect must observe
CURRENT durable authority without the caller manually refreshing first.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.orm import sessionmaker

from app.development_operator.service import OperatorAuthorizationError, write_file
from app.models.mainai_job import MainAIJob
from tests.backend.mainai.test_development_operator import _foundation


def test_stale_session_sees_concurrent_cancel_at_require_context(
    superuser_db, tmp_path
):
    """Two real sessions: B commits cancel; A does not refresh; write must refuse.

    Pre-fix negative control (without populate_existing on _require_context job/task
    selects): Session A's cached job.cancel_requested stays False and the write lands.
    Post-fix: fence reloads durable state → cancel requested → ZERO filesystem effect.
    """
    _, _, _, job, _, context = _foundation(superuser_db, tmp_path)
    superuser_db.commit()
    job_id = job.id
    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)

    session_a = Session()
    session_b = Session()
    try:
        # Session A maps the live job (cancel_requested False) into its identity map.
        job_a = session_a.get(MainAIJob, job_id)
        assert job_a is not None
        assert job_a.cancel_requested is False

        target = context.repository_root / "safe.txt"
        original = target.read_text(encoding="utf-8")
        before = hashlib.sha256(original.encode()).hexdigest()

        # Session B commits a real founder cancel.
        job_b = session_b.get(MainAIJob, job_id)
        assert job_b is not None
        job_b.cancel_requested = True
        session_b.commit()

        # Session A must NOT manually refresh — the fence itself must see B's commit.
        assert job_a.cancel_requested is False  # still stale in identity map pre-fence
        with pytest.raises(OperatorAuthorizationError, match="cancel requested"):
            write_file(
                session_a,
                context,
                path="safe.txt",
                content="should-not-land\n",
                expected_sha256=before,
                idempotency_key="stale-session-a-after-cancel",
            )
        assert target.read_text(encoding="utf-8") == original
    finally:
        session_a.close()
        session_b.close()


def test_stale_session_sees_concurrent_lease_takeover_at_require_context(
    superuser_db, tmp_path
):
    """Session B advances lease generation; Session A's stale context must get ZERO effect."""
    _, _, _, job, worktree, context = _foundation(superuser_db, tmp_path)
    superuser_db.commit()
    job_id = job.id
    worktree_id = worktree.id
    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)

    session_a = Session()
    session_b = Session()
    try:
        job_a = session_a.get(MainAIJob, job_id)
        assert job_a is not None
        assert job_a.lease_generation == context.lease_generation

        target = context.repository_root / "safe.txt"
        original = target.read_text(encoding="utf-8")
        before = hashlib.sha256(original.encode()).hexdigest()

        job_b = session_b.get(MainAIJob, job_id)
        from app.models.mainai_recovery import MainAITaskWorktree

        wt_b = session_b.get(MainAITaskWorktree, worktree_id)
        assert job_b is not None and wt_b is not None
        job_b.locked_by = "worker-takeover"
        job_b.lease_generation = context.lease_generation + 1
        wt_b.lease_generation = context.lease_generation + 1
        session_b.commit()

        assert job_a.lease_generation == context.lease_generation
        with pytest.raises(OperatorAuthorizationError, match="stale or absent"):
            write_file(
                session_a,
                context,
                path="safe.txt",
                content="stale-worker-must-not-write\n",
                expected_sha256=before,
                idempotency_key="stale-session-a-after-takeover",
            )
        assert target.read_text(encoding="utf-8") == original
    finally:
        session_a.close()
        session_b.close()


def test_authorized_same_session_write_still_succeeds(superuser_db, tmp_path):
    """Positive control: live authority + fresh fence still permits a normal write."""
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    target = context.repository_root / "safe.txt"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    result = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content="authorized-write\n",
        expected_sha256=before,
        idempotency_key="fresh-fence-positive",
    )
    assert result.result == "succeeded"
    assert target.read_text(encoding="utf-8") == "authorized-write\n"
