"""Stage C memory → work linkage."""

from app.memory_work_linkage.service import (
    MemoryWorkLinkageError,
    apply_memory_work_linkage,
    assert_no_forbidden_imports,
    find_affected_work,
)
from app.memory_work_linkage.types import ImpactKind, LinkageAction, TimingClass

__all__ = [
    "ImpactKind",
    "LinkageAction",
    "MemoryWorkLinkageError",
    "TimingClass",
    "apply_memory_work_linkage",
    "assert_no_forbidden_imports",
    "find_affected_work",
]
