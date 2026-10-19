"""Founder-facing application-readiness ops: truthful status, STOP, spend, no fabricated GO."""

from app.mainai_founder_ops.assignment_guard import (
    DuplicateWorkError,
    assert_agent_dispatch_allowed,
    assert_no_duplicate_task_assignment,
)
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import FounderWorkStatus

__all__ = [
    "DuplicateWorkError",
    "FounderWorkStatus",
    "assert_agent_dispatch_allowed",
    "assert_no_duplicate_task_assignment",
    "build_founder_ops_snapshot",
]
