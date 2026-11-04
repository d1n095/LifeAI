"""Founder-facing application-readiness ops: truthful status, STOP, spend, no fabricated GO."""

from app.mainai_founder_ops.assignment_guard import (
    CircularDependencyError,
    DuplicateWorkError,
    assert_agent_dispatch_allowed,
    assert_no_circular_dependency,
    assert_no_duplicate_task_assignment,
)
from app.mainai_founder_ops.communication import MAINAI_COMMUNICATION_POLICY, apply_communication_policy
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import FounderWorkStatus

__all__ = [
    "CircularDependencyError",
    "DuplicateWorkError",
    "FounderWorkStatus",
    "MAINAI_COMMUNICATION_POLICY",
    "apply_communication_policy",
    "assert_agent_dispatch_allowed",
    "assert_no_circular_dependency",
    "assert_no_duplicate_task_assignment",
    "build_founder_ops_snapshot",
]
