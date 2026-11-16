"""`app.mainai_cognitive_ops.systemic_debugging` + `compatibility_graph` + `change_impact`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md."""

from __future__ import annotations

import pytest

from app.mainai_cognitive_ops.change_impact import estimate_impact, select_regression_scope, verify_impact
from app.mainai_cognitive_ops.compatibility_graph import CompatibilityEdge, blast_radius, build_compatibility_graph
from app.mainai_cognitive_ops.systemic_debugging import DebugStage, DebugTrace, assess_fix_readiness, claims_local_correctness_only, record_stage


def test_local_test_pass_alone_is_not_sufficient_to_claim_fixed():
    trace = DebugTrace(symptom="crash on empty list")
    for stage in (DebugStage.SYMPTOM, DebugStage.REPRODUCE, DebugStage.ROOT_CAUSE, DebugStage.SMALLEST_SAFE_FIX, DebugStage.LOCAL_TEST):
        trace = record_stage(trace, stage)
    readiness = assess_fix_readiness(trace)
    assert readiness.ready_to_claim_fixed is False
    assert DebugStage.REGRESSION in readiness.missing_stages
    assert claims_local_correctness_only(trace) is True


def test_every_stage_completed_allows_fixed_claim():
    trace = DebugTrace(symptom="x")
    for stage in DebugStage:
        trace = record_stage(trace, stage)
    readiness = assess_fix_readiness(trace)
    assert readiness.ready_to_claim_fixed is True
    assert readiness.missing_stages == ()


def _sample_graph():
    return build_compatibility_graph(
        nodes=("schema_x", "orm_model", "serializer", "api"),
        edges=(
            CompatibilityEdge(producer="schema_x", consumer="orm_model", kind="schema_consumed_by"),
            CompatibilityEdge(producer="orm_model", consumer="serializer", kind="model_serialized_by"),
            CompatibilityEdge(producer="serializer", consumer="api", kind="serialized_by_exposed_via"),
        ),
    )


def test_dangling_edge_reference_is_rejected():
    with pytest.raises(ValueError):
        build_compatibility_graph(nodes=("a",), edges=(CompatibilityEdge(producer="a", consumer="b", kind="calls"),))


def test_schema_change_breaks_downstream_consumer_and_is_caught():
    graph = _sample_graph()
    radius = blast_radius(graph, ("schema_x",))
    assert radius == frozenset({"orm_model", "serializer", "api"})


def test_change_impact_estimate_vs_actual_flags_unexpected_impact():
    graph = _sample_graph()
    estimate = estimate_impact(graph, ("orm_model",))
    verification = verify_impact(estimate, actually_affected=frozenset({"serializer", "api", "unrelated_worker"}))
    assert verification.matches is False
    assert "unrelated_worker" in verification.unexpected_impact


def test_regression_scope_includes_changed_node_and_full_blast_radius():
    graph = _sample_graph()
    scope = select_regression_scope(graph, ("schema_x",))
    assert scope == frozenset({"schema_x", "orm_model", "serializer", "api"})
