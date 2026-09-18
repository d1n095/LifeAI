"""Cognitive Loop -- a thin composition wrapper around the real, already-proven
`app.mainai_executive.loop.run_executive_cycle()` (UNDERSTAND->CONNECT->PLAN->ACT->VERIFY->
STORE->LEARN->REPLAN->CONTINUE, durable via `continuity.py`), adding the vision-graph-specific
steps this program's own reconciliation identified as genuinely missing: recompile the vision
graph, recompute weighted completion, and propose implied requirements -- as explicit, cited
extra steps, never a reimplementation of the underlying cycle.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture
decision this module implements.

VISION != AUTHORITY. MODEL OUTPUT != AUTHORITY. This module authorizes nothing beyond what
`run_executive_cycle()` itself already refuses to authorize (see that function's own
`AUTHORITY_DENIALS` list, carried through unchanged on `CognitiveLoopResult.executive_result`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.loop import run_executive_cycle
from app.mainai_executive.types import ExecutiveCycleResult
from app.mainai_vision.completion import CompletionReport, assess_program_completion
from app.mainai_vision.gap_generator import GapReport, propose_implied_requirements
from app.mainai_vision.types import VisionGraph


@dataclass(frozen=True)
class CognitiveLoopResult:
    executive_result: ExecutiveCycleResult
    vision_graph: VisionGraph
    completion: CompletionReport
    gap_report: GapReport
    authority_denials: tuple[str, ...]


def run_cognitive_cycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_request: str,
    session_id: str | None = None,
    source_entity_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
    **executive_cycle_kwargs: Any,
) -> CognitiveLoopResult:
    """Runs the real `run_executive_cycle()` UNCHANGED, then adds:
      - CONNECT TO WHOLE VISION: recompile the current `VisionGraph` for this owner.
      - RECOMPUTE COMPLETION: `assess_program_completion()` against that fresh graph.
      - IDENTIFY GAPS: `propose_implied_requirements()` against the founder_request text (pure,
        never persisted here -- a caller wanting these durable calls
        `gap_generator.persist_gap_proposals()` explicitly, separately)."""

    from app.mainai_vision.vision_compiler import compile_vision_graph

    executive_result = run_executive_cycle(
        db, owner_id=owner_id, founder_request=founder_request, session_id=session_id,
        source_entity_id=source_entity_id, note_id=note_id, **executive_cycle_kwargs,
    )
    vision_graph = compile_vision_graph(db, owner_id=owner_id)
    completion = assess_program_completion(db, owner_id=owner_id)
    gap_report = propose_implied_requirements(capability_description=founder_request)

    return CognitiveLoopResult(
        executive_result=executive_result,
        vision_graph=vision_graph,
        completion=completion,
        gap_report=gap_report,
        authority_denials=tuple(executive_result.authority_denials) + ("VISION_GRAPH_IS_NOT_AUTHORITY", "COMPLETION_REPORT_IS_NOT_AUTHORITY", "GAP_PROPOSAL_IS_NOT_AUTHORITY"),
    )
