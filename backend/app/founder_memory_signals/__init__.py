"""Deterministic recording/query API for Life's candidate learning signal staging layer --
the boundary between a live signal producer (a heuristic classifier) and trusted founder
truth (`app.founder_memory`). See `service.py`'s own module docstring for the full doctrine."""

from app.founder_memory_signals.service import (
    CandidateLearningSignalError,
    dismiss_candidate_signal,
    get_candidate_signal,
    list_candidate_signals,
    list_unreviewed_candidate_signals,
    promote_candidate_signal,
    record_candidate_signal,
)

__all__ = [
    "CandidateLearningSignalError",
    "dismiss_candidate_signal",
    "get_candidate_signal",
    "list_candidate_signals",
    "list_unreviewed_candidate_signals",
    "promote_candidate_signal",
    "record_candidate_signal",
]
