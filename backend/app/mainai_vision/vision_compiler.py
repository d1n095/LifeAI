"""Vision Compiler -- compiles current, valid `project_entities` rows (migration 0072's widened
vocabulary) into one canonical, typed `VisionGraph`. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

DERIVE, NEVER DUPLICATE: this module never stores a second copy of the graph -- `compile_vision_
graph()` is a plain, read-only compile-at-call-time function, exactly like `app.resource_
intelligence.scheduler.next_best_resource_allocation()`'s own "composition layer, not a cache"
precedent. VISION != AUTHORITY: zero writes anywhere in this module (verified structurally by
this package's own test, mirroring `resource_intelligence.decision`'s AST-based purity proof).

Reject or downgrade rejected/superseded/obsolete/contradicted/unsupported/stale/unauthorized:
`EXCLUDED_ENTITY_STATUSES` (historical/superseded/disputed) are filtered out entirely, matching
`project_entities.service.list_current_project_entities()`'s own "current" definition verbatim
-- this module never invents a second, different notion of "current"."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_executive.why_graph import why_feature_exists
from app.models.project_entities import ProjectEntity, ProjectEntityRelationship
from app.mainai_vision.types import (
    EXCLUDED_ENTITY_STATUSES,
    VisionEdge,
    VisionEdgeKind,
    VisionGraph,
    VisionNode,
    VisionNodeKind,
)

# The subset of project_entities.entity_type this package's own vision graph is compiled from --
# `vision_statement` (pre-existing) plus migration 0072's ten new values. Deliberately excludes
# `idea`/`decision`/`task_reference`/`open_question` -- those remain the founder-memory/why-graph
# domain's own concern (`why_graph.py`), not double-counted here as vision nodes; a caller
# wanting THOSE still has `app.project_entities.service.list_current_project_entities()` directly.
_VISION_ENTITY_TYPES = frozenset(kind.value for kind in VisionNodeKind)
_VISION_RELATIONSHIP_TYPES = frozenset(kind.value for kind in VisionEdgeKind)


def compile_vision_graph(db: Session, *, owner_id: uuid.UUID, include_why: bool = False) -> VisionGraph:
    """Compiles the CURRENT vision graph for one owner. `include_why` additionally calls the
    real, existing `why_graph.why_feature_exists()` per node (bounded by node count; opt-in
    because it is one extra query per node) -- never re-implements WHY-chain logic here."""

    compiled_at = datetime.now(timezone.utc)
    all_rows = list(
        db.execute(
            select(ProjectEntity).where(
                ProjectEntity.owner_id == owner_id,
                ProjectEntity.entity_type.in_(_VISION_ENTITY_TYPES),
            )
        ).scalars()
    )
    current_rows = [r for r in all_rows if r.status not in EXCLUDED_ENTITY_STATUSES]
    excluded_count = len(all_rows) - len(current_rows)
    current_ids = {r.id for r in current_rows}

    nodes: list[VisionNode] = []
    for row in current_rows:
        why = None
        if include_why:
            evidence = why_feature_exists(db, owner_id=owner_id, note_id=None, work_candidate_id=None)
            why = evidence.get("chain")[0]["content_preview"] if evidence.get("chain") else None
        nodes.append(
            VisionNode(
                entity_id=row.id,
                kind=VisionNodeKind(row.entity_type),
                title=row.title,
                summary=row.summary,
                status=row.status,
                authority=row.authority,
                basis=row.basis,
                confidence=row.confidence,
                currentness="current",
                source_claim_id=row.derived_from_claim_id,
                supersedes_entity_id=row.supersedes_entity_id,
                created_at=row.created_at,
                why=why,
                provenance=dict(row.provenance or {}),
            )
        )

    edges: list[VisionEdge] = []
    if current_ids:
        relationship_rows = list(
            db.execute(
                select(ProjectEntityRelationship).where(
                    ProjectEntityRelationship.owner_id == owner_id,
                    ProjectEntityRelationship.relationship_type.in_(_VISION_RELATIONSHIP_TYPES),
                    ProjectEntityRelationship.from_entity_id.in_(current_ids),
                    ProjectEntityRelationship.to_entity_id.in_(current_ids),
                )
            ).scalars()
        )
        for row in relationship_rows:
            edges.append(
                VisionEdge(
                    from_entity_id=row.from_entity_id,
                    to_entity_id=row.to_entity_id,
                    kind=VisionEdgeKind(row.relationship_type),
                    note=row.note,
                )
            )

    return VisionGraph(
        owner_id=owner_id,
        compiled_at=compiled_at,
        nodes=tuple(nodes),
        edges=tuple(edges),
        excluded_count=excluded_count,
    )
