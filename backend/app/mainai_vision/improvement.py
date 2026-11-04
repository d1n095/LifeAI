"""Continuous Improvement -- maintains BUILD_LOOP and IMPROVEMENT_LOOP as separate concerns. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

100% OF CURRENT VISION != NOTHING MORE CAN BE IMPROVED, exercised explicitly below: reaching
100% completion (per `completion.py`) never stops this module from proposing further work --
it only changes WHICH loop that work belongs to (IMPROVEMENT_LOOP, not BUILD_LOOP).

Composes `app.mainai_executive.meta_improvement.py` for the lesson-candidate side (behavioral
corrections) unchanged -- never re-implements `EngineeringLesson`/`lesson_conflicts` handling."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class LoopKind(str, enum.Enum):
    BUILD_LOOP = "BUILD_LOOP"
    IMPROVEMENT_LOOP = "IMPROVEMENT_LOOP"


class ImprovementCategory(str, enum.Enum):
    BOTTLENECK = "bottleneck"
    RISK = "risk"
    SIMPLIFICATION = "simplification"
    COST = "cost"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"
    ARCHITECTURAL_ENTROPY = "architectural_entropy"
    NEW_FOUNDER_CONTEXT = "new_founder_context"


@dataclass(frozen=True)
class ImprovementProposal:
    category: ImprovementCategory
    title: str
    rationale: str
    loop: LoopKind
    authorized: bool = False


def choose_loop(*, overall_completion_percent: float) -> LoopKind:
    """The ONE, single, deterministic rule this module uses to route work: below 100%
    completion (per `completion.py`'s own definition, not a task count), new work belongs to
    BUILD_LOOP; at or above 100%, further work belongs to IMPROVEMENT_LOOP. Reaching 100% does
    NOT mean routing stops -- it means routing changes lane."""

    return LoopKind.BUILD_LOOP if overall_completion_percent < 100.0 else LoopKind.IMPROVEMENT_LOOP


def propose_next_improvement(
    *,
    overall_completion_percent: float,
    candidates: tuple[ImprovementProposal, ...],
) -> tuple[LoopKind, ImprovementProposal | None]:
    """Pure: no `db`, no I/O. Returns the ACTIVE loop for the current completion state plus the
    single highest-priority candidate proposal already routed to that loop (never authorizes
    it) -- `candidates` themselves are the caller's own observations (bottleneck/risk/
    simplification/etc; this module does not invent them, matching `judgment.py`'s own
    "caller supplies the real signal" convention)."""

    loop = choose_loop(overall_completion_percent=overall_completion_percent)
    matching = tuple(c for c in candidates if c.loop == loop)
    return loop, (matching[0] if matching else None)


def new_founder_context_reduces_completion(*, before_percent: float, after_percent: float) -> bool:
    """A trivial, explicit assertion helper mirroring the founder's own explicitly-required test
    scenario -- kept here (not only in a test file) so any future caller of this module can
    re-check the same invariant against real, live numbers, not just the fixed test scenario."""

    return after_percent < before_percent
