"""Crash after local write before durable Operator audit / verify.

Simulates: filesystem effect landed, process died before work_trace commit.
Resume with the same idempotency key must heal from on-disk content — not raise
expected-before hash mismatch, and not rewrite/corrupt the file.

Heal is bound to durable write-intent identity (idempotency_key + job/task/path/
after_hash). Matching bytes alone must not let a different semantic operation steal
the prior write's missing audit.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from app.development_operator.service import (
    OperatorAuthorizationError,
    OperatorPathError,
    _record_write_intent,
    write_file,
)
from tests.backend.mainai.test_development_operator import _foundation


def _simulate_crash_after_write(context, *, path, content, before, idempotency_key):
    """Durable intent + FS write, no Operator audit — the #183 crash window."""
    after = hashlib.sha256(content.encode()).hexdigest()
    target = context.repository_root / path
    _record_write_intent(
        context,
        idempotency_key=idempotency_key,
        path=path,
        capability="patch_file",
        before_sha256=before,
        after_sha256=after,
    )
    target.write_text(content, encoding="utf-8")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == after
    return after


def test_crash_after_disk_write_before_audit_heals_from_on_disk_content(
    superuser_db, tmp_path
):
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    new_content = "changed-after-crash\n"
    after = _simulate_crash_after_write(
        context,
        path="safe.txt",
        content=new_content,
        before=before,
        idempotency_key="crash-write-1",
    )
    target = context.repository_root / "safe.txt"

    result = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content=new_content,
        expected_sha256=before,  # pre-crash before-hash; on-disk no longer matches
        idempotency_key="crash-write-1",
    )
    assert result.result == "succeeded"
    assert result.detail["after_sha256"] == after
    assert target.read_text(encoding="utf-8") == new_content

    # Idempotent resume — same key, no second mutation.
    replay = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content=new_content,
        expected_sha256=before,
        idempotency_key="crash-write-1",
    )
    assert replay.trace_event_id == result.trace_event_id
    assert target.read_text(encoding="utf-8") == new_content


def test_different_idempotency_key_cannot_heal_matching_bytes(
    superuser_db, tmp_path
):
    """Negative control: Operation Y must not steal Operation X's crash-heal.

    Pre-fix: disk already H at P was enough to heal under any idempotency_key.
    Post-fix: without X's write-intent identity, Y hits expected-before mismatch.
    """
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    new_content = "same-bytes-different-operation\n"
    _simulate_crash_after_write(
        context,
        path="safe.txt",
        content=new_content,
        before=before,
        idempotency_key="operation-x",
    )
    target = context.repository_root / "safe.txt"
    content_before_y = target.read_text(encoding="utf-8")

    with pytest.raises(OperatorAuthorizationError, match="expected-before hash mismatch"):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content=new_content,
            expected_sha256=before,
            idempotency_key="operation-y",
        )

    assert target.read_text(encoding="utf-8") == content_before_y
    from app.models.work_intelligence import WorkTraceEvent

    y_events = (
        superuser_db.query(WorkTraceEvent)
        .filter(WorkTraceEvent.idempotency_key == "operation-y")
        .all()
    )
    assert y_events == []

    # Same operation identity still heals.
    healed = write_file(
        superuser_db,
        context,
        path="safe.txt",
        content=new_content,
        expected_sha256=before,
        idempotency_key="operation-x",
    )
    assert healed.result == "succeeded"


def test_matching_bytes_without_write_intent_do_not_heal(superuser_db, tmp_path):
    """Bytes alone (no durable intent) must not open the heal shortcut."""
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    new_content = "orphaned-bytes\n"
    target = context.repository_root / "safe.txt"
    target.write_text(new_content, encoding="utf-8")

    with pytest.raises(OperatorAuthorizationError, match="expected-before hash mismatch"):
        write_file(
            superuser_db,
            context,
            path="safe.txt",
            content=new_content,
            expected_sha256=before,
            idempotency_key="orphan-heal",
        )


def test_second_worker_resuming_after_lease_takeover_cannot_heal_a_write_outside_its_own_current_scope(
    superuser_db, tmp_path
):
    """Attack area 3 composition: worker A's write lands on disk, A crashes before the
    Operator audit commits, the lease is taken over by worker B (a real, different
    worker_id/lease_generation -- not the same worker resuming), and B's OWN current
    execution scope is NARROWER than A's was (does not include the path A already wrote).
    B must never benefit from #183's crash-heal shortcut to silently complete a write
    outside its own current authority -- CURRENT DURABLE AUTHORITY governs recovery, never
    a scope that happened to be valid for whoever wrote the bytes on disk."""
    owner, goal, task, job, worktree, context_a = _foundation(superuser_db, tmp_path)
    assert "safe.txt" in context_a.allowed_paths

    before = hashlib.sha256(b"canonical\n").hexdigest()
    new_content = "changed-by-worker-a-before-crash\n"
    target = context_a.repository_root / "safe.txt"

    _simulate_crash_after_write(
        context_a,
        path="safe.txt",
        content=new_content,
        before=before,
        idempotency_key="crash-write-1",
    )

    # Real lease takeover: a DIFFERENT worker_id/lease_generation, matching how
    # claim_supervisor_goal_lease()/job-lease reclaim actually advances these fields --
    # including the per-job recovery worktree's own generation, exactly as a real takeover
    # would rebind it (this test targets the SCOPE-narrowing question, not worktree-identity
    # semantics, which are already covered elsewhere).
    job.locked_by = "worker-2"
    job.lease_generation = 8
    worktree.lease_generation = 8
    superuser_db.flush()

    context_b = replace(
        context_a,
        worker_id="worker-2",
        lease_generation=8,
        # Worker B's own current envelope does NOT authorize safe.txt -- narrower than A's.
        allowed_paths=("test_operator_sample.py",),
    )

    with pytest.raises(OperatorPathError, match="outside the authorized path scope"):
        write_file(
            superuser_db,
            context_b,
            path="safe.txt",
            content=new_content,
            expected_sha256=before,
            idempotency_key="crash-write-1",
        )

    # No silent heal, no audit event attributing this to worker B, file untouched further.
    assert target.read_text(encoding="utf-8") == new_content
    from app.models.work_intelligence import WorkTraceEvent

    events = (
        superuser_db.query(WorkTraceEvent)
        .filter(
            WorkTraceEvent.owner_id == owner.id,
            WorkTraceEvent.strategy_execution_id == context_a.strategy_execution_id,
            WorkTraceEvent.idempotency_key == "crash-write-1",
        )
        .all()
    )
    assert events == []

    # Isolate the one real variable: if worker B's OWN current envelope had legitimately
    # included safe.txt (everything else about B -- worker_id, lease_generation 8 -- held
    # constant), the heal succeeds. Proves the rejection above was really about B's narrower
    # scope specifically, not some unrelated break in the crash-heal mechanism itself.
    # Intent was recorded under A's worker identity fields that are NOT part of the
    # identity match (worker_id/lease_generation intentionally excluded from binding so
    # legitimate takeover with same job/task/key can heal under current authority).
    context_b_with_scope = replace(context_b, allowed_paths=("safe.txt",))
    healed = write_file(
        superuser_db,
        context_b_with_scope,
        path="safe.txt",
        content=new_content,
        expected_sha256=before,
        idempotency_key="crash-write-1",
    )
    assert healed.result == "succeeded"
    assert healed.detail["after_sha256"] == hashlib.sha256(new_content.encode()).hexdigest()
