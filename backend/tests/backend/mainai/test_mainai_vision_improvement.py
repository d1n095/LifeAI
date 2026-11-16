"""MainAI Cognitive Control Plane -- `app.mainai_vision.improvement` -- proves 100% OF CURRENT
VISION != NOTHING MORE CAN BE IMPROVED: reaching 100% completion routes work to IMPROVEMENT_LOOP,
never stops it, and improvement candidates keep flowing.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

from app.mainai_vision.improvement import (
    ImprovementCategory,
    ImprovementProposal,
    LoopKind,
    choose_loop,
    new_founder_context_reduces_completion,
    propose_next_improvement,
)


def test_below_100_routes_to_build_loop():
    assert choose_loop(overall_completion_percent=87.0) == LoopKind.BUILD_LOOP


def test_at_100_routes_to_improvement_loop_not_stopped():
    assert choose_loop(overall_completion_percent=100.0) == LoopKind.IMPROVEMENT_LOOP


def test_100_percent_still_finds_a_bottleneck_to_propose():
    """THE explicit scenario: system reaches 100%, then improvement loop finds a performance
    weakness -- must still surface a real candidate, not silently stop."""
    candidates = (
        ImprovementProposal(category=ImprovementCategory.PERFORMANCE, title="slow query", rationale="p95 latency regressed", loop=LoopKind.IMPROVEMENT_LOOP),
    )
    loop, proposal = propose_next_improvement(overall_completion_percent=100.0, candidates=candidates)
    assert loop == LoopKind.IMPROVEMENT_LOOP
    assert proposal is not None
    assert proposal.category == ImprovementCategory.PERFORMANCE
    assert proposal.authorized is False


def test_below_100_never_surfaces_an_improvement_loop_candidate():
    candidates = (
        ImprovementProposal(category=ImprovementCategory.SIMPLIFICATION, title="dead code", rationale="unused module", loop=LoopKind.IMPROVEMENT_LOOP),
    )
    loop, proposal = propose_next_improvement(overall_completion_percent=60.0, candidates=candidates)
    assert loop == LoopKind.BUILD_LOOP
    assert proposal is None  # the only candidate was IMPROVEMENT_LOOP-scoped; none match BUILD_LOOP


def test_no_matching_candidate_is_none_not_fabricated():
    loop, proposal = propose_next_improvement(overall_completion_percent=100.0, candidates=())
    assert proposal is None


def test_new_founder_context_reducing_completion_is_detected():
    assert new_founder_context_reduces_completion(before_percent=100.0, after_percent=91.0) is True
    assert new_founder_context_reduces_completion(before_percent=100.0, after_percent=100.0) is False
