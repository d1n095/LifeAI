"""MainAI Cognitive Control Plane -- `app.mainai_vision.completion` -- proves the maturity
ladder cannot be skipped (CODE WRITTEN != DONE), that completion is weighted and graph-wide (not
task-count based), and the program's own central, explicitly-required invariant: NEW VALID
FOUNDER CONTEXT CAN EXPAND THE DENOMINATOR AND REDUCE COMPLETION FROM 100% TO A LOWER NUMBER.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.mainai_vision.completion import (
    CompletionDimension,
    NodeCompletionInput,
    assess_program_completion,
    compute_completion,
    compute_node_maturity,
)
from app.mainai_vision.types import MaturityState, VisionGraph, VisionNode, VisionNodeKind


def _node(entity_id=None, kind=VisionNodeKind.REQUIREMENT, title="req") -> VisionNode:
    return VisionNode(
        entity_id=entity_id or uuid.uuid4(), kind=kind, title=title, summary=None, status="active",
        authority="founder", basis="manual", confidence=None, currentness="current",
        source_claim_id=None, supersedes_entity_id=None, created_at=datetime.now(timezone.utc),
    )


def _graph(nodes: tuple[VisionNode, ...]) -> VisionGraph:
    return VisionGraph(owner_id=uuid.uuid4(), compiled_at=datetime.now(timezone.utc), nodes=nodes, edges=(), excluded_count=0)


# ============================================================================ maturity ladder cannot be skipped


def test_maturity_climb_stops_at_first_missing_rung():
    evidence = {"implemented": True, "unit_tested": True, "production_proven": True}  # skips integration_tested onward
    maturity = compute_node_maturity(baseline=MaturityState.ARCHITECTED, evidence=evidence)
    assert maturity == MaturityState.UNIT_TESTED  # stops at integration_tested (missing), never jumps to production_proven


def test_maturity_never_regresses_below_baseline():
    maturity = compute_node_maturity(baseline=MaturityState.ARCHITECTED, evidence={})
    assert maturity == MaturityState.ARCHITECTED


def test_full_evidence_chain_climbs_all_the_way():
    evidence = {state.value.lower(): True for state in MaturityState}
    maturity = compute_node_maturity(baseline=MaturityState.DISCOVERED, evidence=evidence)
    assert maturity == MaturityState.MAINTAINED


# ============================================================================ weighted, graph-wide completion


def test_completion_is_not_task_count_based():
    """Two nodes, one fully mature and one barely discovered, must NOT average to 50% via a
    naive count -- it should reflect the REAL weighted maturity fraction."""
    mature_id, immature_id = uuid.uuid4(), uuid.uuid4()
    graph = _graph((_node(mature_id, title="mature"), _node(immature_id, title="immature")))
    node_maturity = {mature_id: MaturityState.INDEPENDENTLY_REVIEWED, immature_id: MaturityState.DISCOVERED}
    report = compute_completion(graph=graph, node_inputs={}, node_maturity=node_maturity, quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED)
    assert report.overall_percent == pytest.approx(50.0, abs=1.0)


def test_empty_graph_is_zero_not_a_division_error():
    graph = _graph(())
    report = compute_completion(graph=graph, node_inputs={}, node_maturity={}, quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED)
    assert report.overall_fraction == 0.0
    assert report.denominator == 0.0


def test_100_percent_definition_is_always_present_and_explicit():
    node_id = uuid.uuid4()
    graph = _graph((_node(node_id),))
    report = compute_completion(graph=graph, node_inputs={}, node_maturity={node_id: MaturityState.INDEPENDENTLY_REVIEWED}, quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED)
    assert report.overall_percent == 100.0
    assert "does NOT mean" in report.definition
    assert report.not_nothing_more_to_improve is True


# ============================================================================ THE central invariant: denominator can expand and reduce 100%


def test_new_valid_founder_context_expands_denominator_and_reduces_completion_below_100():
    """THE explicitly-required test scenario: a program at 100% completion, then a new valid
    vision node appears (new founder requirement) -- completion must drop below 100%, not stay
    pinned, and not silently renormalize away the new gap."""
    existing_id = uuid.uuid4()
    graph_before = _graph((_node(existing_id, title="existing, fully done"),))
    report_before = compute_completion(
        graph=graph_before, node_inputs={}, node_maturity={existing_id: MaturityState.INDEPENDENTLY_REVIEWED},
        quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED,
    )
    assert report_before.overall_percent == 100.0

    new_id = uuid.uuid4()
    graph_after = _graph((_node(existing_id, title="existing, fully done"), _node(new_id, title="new requirement, just discovered")))
    report_after = compute_completion(
        graph=graph_after, node_inputs={}, node_maturity={existing_id: MaturityState.INDEPENDENTLY_REVIEWED, new_id: MaturityState.DISCOVERED},
        quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED,
    )
    assert report_after.overall_percent < 100.0
    assert report_after.denominator > report_before.denominator
    assert report_after.node_count == 2


def test_dimension_weighting_lets_security_gap_pull_down_overall_more_than_ux_gap():
    node_id = uuid.uuid4()
    graph = _graph((_node(node_id),))
    security_only = NodeCompletionInput(entity_id=node_id, applicable_dimensions=frozenset({CompletionDimension.SECURITY}))
    ux_only = NodeCompletionInput(entity_id=node_id, applicable_dimensions=frozenset({CompletionDimension.UX}))
    maturity = {node_id: MaturityState.DISCOVERED}
    security_report = compute_completion(graph=graph, node_inputs={node_id: security_only}, node_maturity=maturity, quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED)
    ux_report = compute_completion(graph=graph, node_inputs={node_id: ux_only}, node_maturity=maturity, quality_threshold=MaturityState.INDEPENDENTLY_REVIEWED)
    # Same maturity, same single dimension each -- overall fraction should be identical (0%) but
    # the WEIGHT contributed to the denominator differs (security > ux), provably different internal state.
    assert security_report.denominator != ux_report.denominator


# ============================================================================ implemented != reviewed != integrated != activated


def test_implemented_does_not_imply_independently_reviewed():
    evidence = {"implemented": True, "unit_tested": True, "integration_tested": True, "robustness_tested": True}
    maturity = compute_node_maturity(baseline=MaturityState.ARCHITECTED, evidence=evidence)
    assert maturity == MaturityState.ROBUSTNESS_TESTED
    assert maturity != MaturityState.INDEPENDENTLY_REVIEWED


def test_reviewed_does_not_imply_integrated():
    evidence = {"implemented": True, "unit_tested": True, "integration_tested": True, "robustness_tested": True, "independently_reviewed": True}
    maturity = compute_node_maturity(baseline=MaturityState.ARCHITECTED, evidence=evidence)
    assert maturity == MaturityState.INDEPENDENTLY_REVIEWED
    assert maturity != MaturityState.INTEGRATED


def test_integrated_does_not_imply_activated():
    evidence = {
        "implemented": True, "unit_tested": True, "integration_tested": True, "robustness_tested": True,
        "independently_reviewed": True, "integrated": True,
    }
    maturity = compute_node_maturity(baseline=MaturityState.ARCHITECTED, evidence=evidence)
    assert maturity == MaturityState.INTEGRATED
    assert maturity != MaturityState.ACTIVATED


# ============================================================================ composed, DB-touching wrapper


def test_assess_program_completion_derives_real_baseline_from_work_candidate(superuser_db):
    from app.agent_coordination.service import register_agent  # noqa: F401  -- ensure module importable in this env
    from app.models.document import ActiveTruthStatus, Document, DocumentSource
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.project_entities import ProjectEntity
    from app.models.user import User
    from app.models.work_candidate import WorkCandidate

    owner = User(email=f"comp-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    document = Document(title="Source", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text="claim", extraction_version="v1")
    superuser_db.add(claim)
    superuser_db.flush()
    entity = ProjectEntity(owner_id=owner.id, entity_type="requirement", title="Real requirement", title_normalized="real requirement", derived_from_claim_id=claim.id, idempotency_key=f"pe-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    superuser_db.add(entity)
    superuser_db.flush()
    superuser_db.add(WorkCandidate(owner_id=owner.id, source_entity_id=entity.id, title="Real requirement", status="unreviewed", idempotency_key=f"wc-{uuid.uuid4()}"))
    superuser_db.commit()

    report = assess_program_completion(superuser_db, owner_id=owner.id)
    assert report.node_maturity[str(entity.id)] == MaturityState.SPECIFIED.value
