"""Composition seams for `app.mainai_coverage`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Real composition: `dynamic_denominator.py` calls the real, unchanged
`app.mainai_vision.gap_generator` staging pipeline. `canonical_vision_texts_snapshot()` below
composes the real `app.mainai_vision.vision_compiler.compile_vision_graph()` to give
`omission_discovery.find_omissions()` a real, current set of canonical-vision text -- never a
second copy of the vision graph.

`source_adapters.py`/`discovery_pipeline.py` now DO real, read-only ingestion of handoff docs,
reconciliation docs, the branch registry, and the git commit log -- a caller no longer has to
hand-compose every `CapabilityClaim`. The one source still NOT real, honestly disclosed: no
durable conversation/chat-history store exists anywhere in this codebase --
`source_adapters.conversation_history_availability()` reports this explicitly rather than
fabricating a read of something that does not exist."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session


def canonical_vision_texts_snapshot(db: Session, *, owner_id: uuid.UUID) -> frozenset[str]:
    from app.mainai_vision.vision_compiler import compile_vision_graph

    graph = compile_vision_graph(db, owner_id=owner_id)
    return frozenset(f"{node.title} {node.summary or ''}".strip() for node in graph.nodes)
