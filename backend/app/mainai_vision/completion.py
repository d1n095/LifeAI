"""Dynamic Completion Engine -- graph-wide, weighted, denominator-expandable completion. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

COMPLETION != TASK COUNT. 100% means COMPLETE AGAINST CURRENT CANONICAL VISION AT CURRENT
VERIFIED QUALITY THRESHOLD -- it does NOT mean nothing more can be improved (see `improvement.py`
for the loop that keeps running at 100%) and it is not fixed: NEW VALID FOUNDER CONTEXT CAN
EXPAND THE DENOMINATOR AND REDUCE COMPLETION FROM 100% TO A LOWER NUMBER -- exercised explicitly
by this module's own tests, never merely asserted.

CODE WRITTEN != DONE, reused from `app.mainai_executive.completion.assess_completion()`'s own
doctrine, not reinvented: a node's maturity can only climb the ladder one confirmed rung at a
time (`compute_node_maturity()` below) -- an `implemented=True` flag with no `unit_tested`
evidence cannot silently imply `production_proven`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.work_candidate import WorkCandidate
from app.mainai_vision.types import (
    MATURITY_INDEX,
    MATURITY_ORDER,
    CompletionDimension,
    MaturityState,
    VisionGraph,
    VisionNode,
)

# Evidence-flag key per maturity rung -- the ladder-climb vocabulary a caller supplies (mirrors
# `mainai_executive.completion.COMPLETION_DIMENSIONS`'s own "caller supplies evidence flags
# explicitly, this module never invents VERIFIED" discipline).
MATURITY_EVIDENCE_KEYS: dict[MaturityState, str] = {state: state.value.lower() for state in MaturityState}

# Default relative weight per CompletionDimension -- documented, hand-picked starting points (no
# historical data exists yet to fit them against), same convention as every other threshold table
# in this program (`decision.py`'s CONTEXT_UTILIZATION_*_PCT, `judgment.py`'s CONFIDENCE_BAR).
# Security/verification/recovery weighted above UX/performance -- a beautiful, fast feature that
# is not safe or recoverable is not "mostly done" in this codebase's own standing doctrine.
DEFAULT_DIMENSION_WEIGHTS: dict[CompletionDimension, float] = {
    CompletionDimension.CAPABILITY: 1.0,
    CompletionDimension.ARCHITECTURE: 1.0,
    CompletionDimension.SECURITY: 1.5,
    CompletionDimension.PRIVACY: 1.5,
    CompletionDimension.INTEGRATION: 1.0,
    CompletionDimension.RECOVERY: 1.25,
    CompletionDimension.VERIFICATION: 1.25,
    CompletionDimension.OPERABILITY: 1.0,
    CompletionDimension.PERFORMANCE: 0.75,
    CompletionDimension.UX: 0.75,
    CompletionDimension.PRODUCTION_READINESS: 1.25,
}


def compute_node_maturity(*, baseline: MaturityState, evidence: dict[str, bool] | None = None) -> MaturityState:
    """Climbs the ladder one confirmed rung at a time starting from `baseline` -- the first
    rung whose evidence flag is missing/False stops the climb; later flags being True cannot
    skip over it. Never regresses below `baseline` (the one real signal always available, see
    `derive_baseline_maturity()`)."""

    evidence = evidence or {}
    best = baseline
    for state in MATURITY_ORDER:
        if MATURITY_INDEX[state] <= MATURITY_INDEX[best]:
            continue
        if not evidence.get(MATURITY_EVIDENCE_KEYS[state], False):
            break
        best = state
    return best


def derive_baseline_maturity(db: Session, *, owner_id: uuid.UUID, node: VisionNode) -> MaturityState:
    """The ONE real signal this module can derive WITHOUT caller-supplied evidence: a linked
    `WorkCandidate` (via `source_entity_id`, real FK, migration 0055) and its own real `status`.
    Everything above SPECIFIED/ARCHITECTED requires explicit caller evidence -- this function
    never guesses IMPLEMENTED/TESTED/REVIEWED/etc from a vision node's own text alone."""

    candidate = db.execute(
        select(WorkCandidate).where(WorkCandidate.owner_id == owner_id, WorkCandidate.source_entity_id == node.entity_id)
    ).scalars().first()
    if candidate is None:
        return MaturityState.UNDERSTOOD if node.status == "active" else MaturityState.DISCOVERED
    if candidate.status == "authorized":
        return MaturityState.ARCHITECTED
    if candidate.status in ("dismissed", "superseded"):
        # A rejected/superseded staging attempt is not forward progress on THIS node.
        return MaturityState.UNDERSTOOD if node.status == "active" else MaturityState.DISCOVERED
    return MaturityState.SPECIFIED  # unreviewed: staged, not yet authorized


@dataclass(frozen=True)
class NodeCompletionInput:
    """Per-node completion inputs a caller supplies -- never invented by this module.
    `evidence` uses `MATURITY_EVIDENCE_KEYS` values. `applicable_dimensions` defaults to ALL 11
    (a node that genuinely has no security surface should explicitly narrow this, not rely on
    a silent default that could hide a missing security review)."""

    entity_id: uuid.UUID
    evidence: dict[str, bool] = field(default_factory=dict)
    applicable_dimensions: frozenset[CompletionDimension] = field(default_factory=lambda: frozenset(CompletionDimension))
    weight: float = 1.0


@dataclass(frozen=True)
class CompletionReport:
    overall_fraction: float
    overall_percent: float
    numerator: float
    denominator: float
    node_maturity: dict[str, str]  # entity_id (str) -> MaturityState.value
    per_dimension_fraction: dict[str, float]
    node_count: int
    quality_threshold: MaturityState
    definition: str
    not_nothing_more_to_improve: bool = True


def _quality_fraction(maturity: MaturityState, quality_threshold: MaturityState) -> float:
    """A node's own contribution: 1.0 once it reaches (or passes) `quality_threshold`, a
    proportional partial credit below that -- never over 1.0 (exceeding the threshold does not
    over-count; see `improvement.py` for what happens to work beyond the current threshold)."""

    threshold_index = MATURITY_INDEX[quality_threshold]
    if threshold_index == 0:
        return 1.0
    return min(1.0, MATURITY_INDEX[maturity] / threshold_index)


def compute_completion(
    *,
    graph: VisionGraph,
    node_inputs: dict[uuid.UUID, NodeCompletionInput],
    node_maturity: dict[uuid.UUID, MaturityState],
    quality_threshold: MaturityState = MaturityState.INDEPENDENTLY_REVIEWED,
    dimension_weights: dict[CompletionDimension, float] | None = None,
) -> CompletionReport:
    """Pure: no `db`, no I/O -- matches `decision.propose_resource_action()`'s own pure-function
    shape. `node_maturity` must already be computed (see `derive_baseline_maturity()` +
    `compute_node_maturity()`, or `assess_program_completion()` below for the composed,
    DB-touching convenience wrapper)."""

    weights = dict(DEFAULT_DIMENSION_WEIGHTS)
    if dimension_weights:
        weights.update(dimension_weights)

    numerator = 0.0
    denominator = 0.0
    per_dimension_num: dict[CompletionDimension, float] = {d: 0.0 for d in CompletionDimension}
    per_dimension_den: dict[CompletionDimension, float] = {d: 0.0 for d in CompletionDimension}

    for node in graph.nodes:
        node_input = node_inputs.get(node.entity_id) or NodeCompletionInput(entity_id=node.entity_id)
        maturity = node_maturity.get(node.entity_id, MaturityState.DISCOVERED)
        fraction = _quality_fraction(maturity, quality_threshold)
        for dimension in node_input.applicable_dimensions:
            w = weights.get(dimension, 1.0) * node_input.weight
            per_dimension_num[dimension] += fraction * w
            per_dimension_den[dimension] += w
            numerator += fraction * w
            denominator += w

    overall_fraction = (numerator / denominator) if denominator > 0 else 0.0
    per_dimension_fraction = {
        d.value: (per_dimension_num[d] / per_dimension_den[d] if per_dimension_den[d] > 0 else 0.0)
        for d in CompletionDimension
    }

    return CompletionReport(
        overall_fraction=overall_fraction,
        overall_percent=round(overall_fraction * 100, 2),
        numerator=numerator,
        denominator=denominator,
        node_maturity={str(k): v.value for k, v in node_maturity.items()},
        per_dimension_fraction=per_dimension_fraction,
        node_count=len(graph.nodes),
        quality_threshold=quality_threshold,
        definition=(
            f"100% means COMPLETE AGAINST CURRENT CANONICAL VISION ({len(graph.nodes)} node(s)) "
            f"AT CURRENT VERIFIED QUALITY THRESHOLD ({quality_threshold.value}) -- it does NOT mean "
            "nothing more can be improved; see improvement.py for the loop that continues at 100%"
        ),
    )


def assess_program_completion(
    db: Session,
    *,
    owner_id: uuid.UUID,
    node_inputs: dict[uuid.UUID, NodeCompletionInput] | None = None,
    quality_threshold: MaturityState = MaturityState.INDEPENDENTLY_REVIEWED,
) -> CompletionReport:
    """Composed, DB-touching convenience wrapper: compiles the current vision graph, derives
    each node's baseline maturity for real, applies any caller-supplied evidence, then calls the
    pure `compute_completion()`. Never writes anything."""

    from app.mainai_vision.vision_compiler import compile_vision_graph

    node_inputs = node_inputs or {}
    graph = compile_vision_graph(db, owner_id=owner_id)
    node_maturity: dict[uuid.UUID, MaturityState] = {}
    for node in graph.nodes:
        node_input = node_inputs.get(node.entity_id)
        baseline = derive_baseline_maturity(db, owner_id=owner_id, node=node)
        node_maturity[node.entity_id] = compute_node_maturity(
            baseline=baseline, evidence=node_input.evidence if node_input else None
        )
    return compute_completion(
        graph=graph, node_inputs=node_inputs, node_maturity=node_maturity, quality_threshold=quality_threshold
    )
