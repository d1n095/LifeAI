"""Phase 4 acceptance: recommendations can say reuse, wait, ask or reject."""

from __future__ import annotations

from app.context_atlas.recommendation import (
    CapabilityFact,
    EngineeringRequest,
    RecommendationOutcome,
    recommend,
)


def _facts(**overrides) -> dict[str, CapabilityFact]:
    base = CapabilityFact(
        component="app.memory",
        present=True,
        wired=True,
        test_referenced=True,
        path="backend/app/memory.py",
        endpoints=("GET /api/memory",),
    )
    facts = {"app.memory": base}
    facts.update(overrides)
    return facts


def test_existing_wired_capability_is_reused():
    result = recommend(
        EngineeringRequest("build a new memory engine", capability_aliases=("memory",)),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.reuse
    assert result.execution_authorized is False
    assert any("app.memory" in item for item in result.evidence)
    assert result.uncertainty == "low"


def test_unwired_capability_is_improved_not_rebuilt():
    facts = _facts(
        **{
            "app.memory": CapabilityFact(
                component="app.memory", present=True, wired=False, test_referenced=True
            )
        }
    )
    result = recommend(
        EngineeringRequest("add memory search", capability_aliases=("memory",)), facts
    )

    assert result.outcome is RecommendationOutcome.improve
    assert "not wired" in result.current_state


def test_reported_failure_triggers_investigation_before_replacement():
    result = recommend(
        EngineeringRequest(
            "the memory engine is broken, replace it",
            capability_aliases=("memory",),
            implies_existing_failure=True,
        ),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.investigate
    assert result.outcome is not RecommendationOutcome.replace
    assert any(alternative.option == "replace" for alternative in result.alternatives)
    assert result.founder_decisions
    assert "retrieval" in result.root_problem


def test_active_assignment_prevents_duplicate_work():
    result = recommend(
        EngineeringRequest(
            "build a memory engine",
            capability_aliases=("memory",),
            active_assignment="claude/ws-a-memory",
        ),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.wait
    assert "claude/ws-a-memory" in result.evidence[0]


def test_conflicting_sources_force_an_explicit_question():
    result = recommend(
        EngineeringRequest(
            "adopt the old stack",
            capability_aliases=("memory",),
            conflicting_sources=True,
        ),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.ask
    assert result.uncertainty == "high"


def test_rejected_decision_is_not_silently_revived():
    result = recommend(
        EngineeringRequest(
            "build a memory engine",
            capability_aliases=("memory",),
            prior_decision="rejected",
        ),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.reject
    assert result.founder_decisions


def test_unmatched_request_asks_instead_of_inventing_a_component():
    result = recommend(
        EngineeringRequest("build a quantum planner", capability_aliases=("unknown-area",)),
        _facts(),
    )

    assert result.outcome is RecommendationOutcome.ask
    assert result.execution_authorized is False
    assert result.founder_decisions


def test_every_recommendation_is_read_only():
    requests = [
        EngineeringRequest("x", capability_aliases=("memory",)),
        EngineeringRequest("x", capability_aliases=("memory",), active_assignment="a"),
        EngineeringRequest("x", conflicting_sources=True),
        EngineeringRequest("x", prior_decision="rejected"),
        EngineeringRequest("x"),
    ]
    for request in requests:
        assert recommend(request, _facts()).execution_authorized is False
