"""Types for the composed MainAI executive loop.

FUTURE PLAN != FUTURE AUTHORITY.
MEMORY != AUTHORITY.
CODE WRITTEN != DONE.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutivePhase(str, Enum):
    UNDERSTAND = "UNDERSTAND"
    CORRECT = "CORRECT"
    CONNECT = "CONNECT"
    PLAN = "PLAN"
    ACT = "ACT"
    VERIFY = "VERIFY"
    STORE = "STORE"
    LEARN = "LEARN"
    REPLAN = "REPLAN"
    CONTINUE = "CONTINUE"


class PlanningHorizon(str, Enum):
    NOW = "NOW"
    NEAR = "NEAR"
    MID = "MID"
    LONG = "LONG"


# WorkCandidate.priority vocabulary (migration 0065) for executive-scan rows.
EXECUTIVE_PRIORITY = frozenset({"NOW", "NEAR", "LATER", "OPTIONAL", "BLOCKED"})

HORIZON_TO_PRIORITY = {
    PlanningHorizon.NOW: "NOW",
    PlanningHorizon.NEAR: "NEAR",
    PlanningHorizon.MID: "LATER",
    PlanningHorizon.LONG: "LATER",
}


@dataclass(frozen=True)
class HorizonItem:
    horizon: PlanningHorizon
    title: str
    rationale: str
    confidence: float | None = None
    dependencies: list[str] = field(default_factory=list)
    # Explicit: planning never authorizes.
    authorized: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContinuityCheckpoint:
    """Durable restart state — process memory is never authority."""

    session_id: str
    phase: ExecutivePhase
    founder_request: str
    context_set_id: str | None
    note_id: str | None
    source_entity_id: str | None
    work_candidate_ids: list[str]
    lesson_ids: list[str]
    horizon_items: list[dict[str, Any]]
    staffing_action: str | None
    workforce_request_id: str | None
    completed: list[str]
    uncertain: list[str]
    remaining: list[str]
    authority_still_valid: bool
    authority_notes: list[str]
    interruption_point: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutiveCycleResult:
    session_id: str
    phase: ExecutivePhase
    context_set_id: uuid.UUID | None
    note_id: uuid.UUID | None
    lesson_ids: list[uuid.UUID]
    work_candidate_ids: list[uuid.UUID]
    horizon_items: list[HorizonItem]
    staffing_action: str | None
    staffing_reason: str | None
    workforce_dry_run: dict[str, Any] | None
    continuity_note_id: uuid.UUID | None
    scan_bound_reached: bool
    observability: dict[str, Any]
    # Always list explicit denial facts — never silent "approved".
    authority_denials: list[str] = field(default_factory=list)
    missing_pieces: list[dict[str, Any]] = field(default_factory=list)
    completion_assessment: dict[str, Any] | None = None
    # Contradiction_refs / completion_assessment feed-only refs for safe_planner — never authority.
    contradiction_refs: list[str] = field(default_factory=list)
    # Local Intelligence School path (local-attempt-first); never provider activation.
    school_path: dict[str, Any] | None = None
