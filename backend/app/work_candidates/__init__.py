"""Deterministic recording/query API for Life's work candidate staging layer -- the boundary
between structured project understanding (`app.project_entities`) and real, governed MainAI
work (`app.mainai_execution.planner.create_goal()`). See `service.py`'s own module docstring
for the full doctrine."""

from app.work_candidates.service import (
    WorkCandidateError,
    authorize_work_candidate,
    dismiss_work_candidate,
    get_work_candidate,
    list_unreviewed_work_candidates,
    list_work_candidates,
    record_work_candidate,
)

__all__ = [
    "WorkCandidateError",
    "authorize_work_candidate",
    "dismiss_work_candidate",
    "get_work_candidate",
    "list_unreviewed_work_candidates",
    "list_work_candidates",
    "record_work_candidate",
]
