"""Stage C — Memory → Work linkage types (dataclasses only; avoid app/schemas.py)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class TimingClass(str, Enum):
    NOW = "now"
    LATER = "later"


class ImpactKind(str, Enum):
    AFFECTS_ACTIVE_TASK = "affects_active_task"
    CORRECTION = "correction"
    SAME_COLLAPSE = "same_collapse"
    COMPLETED_FOLLOWUP = "completed_followup"
    PARK_LATER = "park_later"
    CONTRADICTS_PLAN = "contradicts_plan"
    LINK_ONLY = "link_only"


class LinkageAction(str, Enum):
    LINKED_ONLY = "linked_only"
    CANDIDATE_RECORDED = "candidate_recorded"
    CANDIDATE_SUPERSEDED = "candidate_superseded"
    SUBORDINATE_TASK_INSERTED = "subordinate_task_inserted"
    CONTRADICTION_FLAGGED = "contradiction_flagged"
    NOOP_SAME = "noop_same"


@dataclass
class AffectedWorkRef:
    kind: str  # mainai_goal | mainai_task | work_candidate | project_entity
    id: uuid.UUID
    status: str | None = None
    score: float = 0.0
    reason: str = ""


@dataclass
class LinkageResult:
    note_id: uuid.UUID
    thread_id: uuid.UUID
    timing: TimingClass
    impacts: list[ImpactKind] = field(default_factory=list)
    actions: list[LinkageAction] = field(default_factory=list)
    created_task_ids: list[uuid.UUID] = field(default_factory=list)
    # created_candidate_ids: only IDs created in THIS call (empty on replay)
    created_candidate_ids: list[uuid.UUID] = field(default_factory=list)
    # Stable idempotent contract:
    created_now_ids: list[uuid.UUID] = field(default_factory=list)
    canonical_candidate_ids: list[uuid.UUID] = field(default_factory=list)
    replayed: bool = False
    operation_receipt_id: str | None = None
    affected: list[AffectedWorkRef] = field(default_factory=list)
