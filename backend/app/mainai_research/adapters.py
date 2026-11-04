"""Integration seams. See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for
the architecture decision.

RUNTIME STATE != REASONING STATE. RESEARCH != AUTHORITY. Real, composed adapters to
`app.mainai_vision`/`app.resource_intelligence` (both real, DB-backed, present on this branch);
typed `Protocol` seams for Development Director / Continuous Supervision / Personal Recall / V1
readiness (same honest not-yet-wireable disclosure as `app.mainai_vision.adapters`)."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.orm import Session


def vision_completion_snapshot(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """Real composition with `app.mainai_vision.completion.assess_program_completion()` --
    read-only, never re-implemented."""

    from app.mainai_vision.completion import assess_program_completion

    report = assess_program_completion(db, owner_id=owner_id)
    return {
        "overall_percent": report.overall_percent, "node_count": report.node_count,
        "definition": report.definition,
    }


def resource_intelligence_cost_signal(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    """Real composition with `app.resource_intelligence.cost_bridge.cost_per_accepted_commit()`
    -- feeds `provider_economics.py`'s own real signal input, never a second cost computation."""

    from app.resource_intelligence.cost_bridge import cost_per_accepted_commit

    envelope = cost_per_accepted_commit(db, owner_id=owner_id, agent_id=agent_id)
    return {"value": envelope.value, "missing_data": envelope.missing_data, "sample_size": envelope.sample_size}


class DevDirectorAdapter(Protocol):
    def program_status(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...


class ContinuousSupervisionAdapter(Protocol):
    def telemetry_snapshot(self, *, owner_id: uuid.UUID, attempt_id: str) -> dict[str, Any]: ...


class PersonalRecallAdapter(Protocol):
    def recall_status(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...


class V1ReadinessAdapter(Protocol):
    def readiness_report(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...
