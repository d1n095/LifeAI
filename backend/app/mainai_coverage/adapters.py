"""Composition seams for `app.mainai_coverage`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Real composition: `dynamic_denominator.py` calls the real, unchanged
`app.mainai_vision.gap_generator` staging pipeline. `canonical_vision_texts_snapshot()` below
composes the real `app.mainai_vision.vision_compiler.compile_vision_graph()` to give
`omission_discovery.find_omissions()` a real, current set of canonical-vision text -- never a
second copy of the vision graph.

NOT YET REAL, honestly disclosed: no corpus/conversation-history/commit-log scanner exists
anywhere in this codebase that could automatically produce `CapabilityClaim` rows -- a caller
supplies them today (see `types.CapabilityClaim`'s own docstring)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session


def canonical_vision_texts_snapshot(db: Session, *, owner_id: uuid.UUID) -> frozenset[str]:
    from app.mainai_vision.vision_compiler import compile_vision_graph

    graph = compile_vision_graph(db, owner_id=owner_id)
    return frozenset(f"{node.title} {node.summary or ''}".strip() for node in graph.nodes)
