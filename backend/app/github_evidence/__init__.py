"""Deterministic recording/query API for Life's GitHub evidence staging layer -- the boundary
between real, verified GitHub evidence (`app.safe_planner.github_work_queue`) and real,
governed MainAI work (`app.mainai_execution.planner.create_goal()`). See `service.py`'s own
module docstring for the full doctrine."""

from app.github_evidence.service import (
    GithubEvidenceError,
    authorize_github_evidence_candidate,
    candidate_idempotency_key,
    dismiss_github_evidence_candidate,
    get_github_evidence_candidate,
    list_github_evidence_candidates,
    record_github_evidence_candidates,
)

__all__ = [
    "GithubEvidenceError",
    "authorize_github_evidence_candidate",
    "candidate_idempotency_key",
    "dismiss_github_evidence_candidate",
    "get_github_evidence_candidate",
    "list_github_evidence_candidates",
    "record_github_evidence_candidates",
]
