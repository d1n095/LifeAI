"""Integration seams for the six named sibling programs. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

RUNTIME STATE != REASONING STATE. MEMORY != AUTHORITY. METRIC != AUTHORITY. VISION != AUTHORITY.
PROFILE != AUTHORITY -- this module never imports a mutating function from any of the six
programs; every real composition below is READ-ONLY.

Two of the six (Founder Reasoning, Resource Intelligence) are REAL, already-merged, DB-backed
code on this same branch and are composed with directly. Development Director's own module
(`app.dev_director`) IS present in this checkout (confirmed by direct inspection) but is its own
frozen, unreviewed candidate with NO durable Postgres store of its own -- its real functions
(e.g. `founder_brief.generate_founder_brief()`) are pure aggregations over caller-supplied
`Program`/`Job` dataclasses, not something this package can query by `owner_id` alone; wiring a
real adapter needs dev_director's own review/activation first, not just an import. Continuous
Supervision and Personal Recall are separate, unmerged Codex lanes NOT present on this branch at
all (confirmed: neither module exists in this checkout). V1 readiness/evidence was not audited
in this reconciliation pass. All four remaining adapters here are typed `Protocol` interfaces
only, honestly disclosed as not-yet-wireable, never a fabricated live connection."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.orm import Session


# ============================================================================ REAL, composed adapters (same branch)


def founder_reasoning_snapshot(db: Session, *, owner_id: uuid.UUID, session_id: str | None = None) -> dict[str, Any]:
    """Real composition with `app.mainai_executive.observability.executive_status_snapshot()`
    (Founder Reasoning + Judgment, verified SHA `4814d78`) -- read-only, never re-implemented."""

    from app.mainai_executive.observability import executive_status_snapshot

    return executive_status_snapshot(db, owner_id=owner_id, session_id=session_id)


def resource_intelligence_snapshot(db: Session, *, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Real composition with `app.resource_intelligence.scheduler.next_best_resource_allocation()`
    (this program's own prior round) -- read-only, never re-implemented."""

    from app.resource_intelligence import next_best_resource_allocation

    return next_best_resource_allocation(db, owner_id=owner_id)


# ============================================================================ typed seams (not present on this branch)


class DevDirectorAdapter(Protocol):
    """Typed seam for `app.dev_director` (frozen candidate `ab1c0ce...`, module present in this
    checkout but not DB-backed/queryable by `owner_id` -- see module docstring). This Protocol
    documents the shape a real adapter would need once that candidate is reviewed/activated with
    a durable store. No implementation here calls anything real."""

    def program_status(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...


class ContinuousSupervisionAdapter(Protocol):
    """Typed seam for Codex's Continuous Supervision candidate (`a7df7f9...`) -- NOT present on
    this branch (separate, unmerged lane, independently examined and BLOCKED pending
    reconciliation -- see this program's own prior review). No implementation here calls
    anything real; wiring requires that candidate to actually merge first."""

    def telemetry_snapshot(self, *, owner_id: uuid.UUID, attempt_id: str) -> dict[str, Any]: ...


class PersonalRecallAdapter(Protocol):
    """Typed seam for the Personal Recall foundation (`0248355...`) -- NOT present on this
    branch (separate, unmerged lane, independently verified FOUNDATION/SAFE-TO-KEEP-FROZEN with
    two open P0s -- see this program's own prior review). No implementation here calls anything
    real; wiring requires the encryption/production-auth P0s to close first."""

    def recall_status(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...


class V1ReadinessAdapter(Protocol):
    """Typed seam for the V1 readiness/evidence system -- not audited in this reconciliation
    pass (out of scope); documented here as a known integration point for a future pass."""

    def readiness_report(self, *, owner_id: uuid.UUID) -> dict[str, Any]: ...


NOT_YET_WIREABLE = (
    "app.dev_director (present, not DB-backed/reviewed)",
    "codex/mainai-continuous-supervision (not present on this branch)",
    "codex/universal-personal-recall (not present on this branch)",
    "v1_readiness_evidence_system (not audited this pass)",
)
