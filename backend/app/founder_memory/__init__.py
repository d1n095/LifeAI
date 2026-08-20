"""Deterministic recording/query API for Life's own model of the founder/user -- explicit
decisions, corrections, preferences, expressed goals, and observed recurring patterns, always
evidence-backed and never silently promoted in authority. See `service.py`'s own module
docstring for the full doctrine."""

from app.founder_memory.service import (
    FounderMemoryError,
    get_founder_memory,
    list_current_founder_memory,
    list_founder_memory,
    mark_founder_memory_disputed,
    record_founder_memory,
)

__all__ = [
    "FounderMemoryError",
    "get_founder_memory",
    "list_current_founder_memory",
    "list_founder_memory",
    "mark_founder_memory_disputed",
    "record_founder_memory",
]
