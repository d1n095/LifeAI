"""Crash after local write before durable Operator audit / verify.

Simulates: filesystem effect landed, process died before work_trace commit.
Resume with the same idempotency key must heal from on-disk content — not raise
expected-before hash mismatch, and not rewrite/corrupt the file.
"""

from __future__ import annotations

import hashlib

from app.development_operator.service import write_file
from tests.backend.mainai.test_development_operator import _foundation


def test_crash_after_disk_write_before_audit_heals_from_on_disk_content(
    superuser_db, tmp_path
):
    _, _, _, _, _, context = _foundation(superuser_db, tmp_path)
    before = hashlib.sha256(b"canonical\n").hexdigest()
    new_content = "changed-after-crash\n"
    after = hashlib.sha256(new_content.encode()).hexdigest()
    target = context.repository_root / "safe.txt"

    # Simulate crash window: durable FS write without Operator audit row.
    target.write_text(new_content, encoding="utf-8")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == after

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
