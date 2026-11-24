"""Phase 2 acceptance: forgotten requirements are a review queue, not a truth claim."""

from __future__ import annotations

from app.context_atlas.traceability import (
    ContextClass,
    EvidenceRef,
    Stage,
    build_trace,
    forgotten_requirements,
)


def test_approved_decision_without_implementation_is_forgotten():
    trace = build_trace(
        "REQ-APPROVED",
        "Approved but unbuilt",
        "lifeai",
        evidence={
            Stage.vision: (EvidenceRef("doc", "docs/vision.md", True),),
            Stage.decision: (EvidenceRef("founder_decision", "decision:1", True),),
        },
    )

    assert trace.classification is ContextClass.founder_approved_decision
    assert forgotten_requirements([trace]) == (trace,)


def test_implemented_requirement_is_not_forgotten():
    trace = build_trace(
        "REQ-BUILT",
        "Already built",
        "lifeai",
        evidence={
            Stage.decision: (EvidenceRef("founder_decision", "decision:2", True),),
            Stage.implementation: (EvidenceRef("code", "backend/app/x.py"),),
        },
    )

    assert trace.classification is ContextClass.active_work
    assert forgotten_requirements([trace]) == ()


def test_rejected_and_superseded_are_not_forgotten():
    rejected = build_trace(
        "REQ-REJECTED",
        "Rejected",
        "lifeai",
        evidence={Stage.decision: (EvidenceRef("founder_decision", "decision:3", True),)},
        rejected=True,
    )
    superseded = build_trace(
        "REQ-SUPERSEDED",
        "Superseded",
        "lifeai",
        evidence={Stage.decision: (EvidenceRef("founder_decision", "decision:4", True),)},
        superseded_by="decision:5",
    )

    assert forgotten_requirements([rejected, superseded]) == ()


def test_historical_suggestion_is_queued_with_high_uncertainty():
    trace = build_trace(
        "REQ-OLD",
        "Old idea",
        "project-a",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/old.md"),)},
    )

    queued = forgotten_requirements([trace])

    assert queued == (trace,)
    assert queued[0].uncertainty == "high"
    assert queued[0].canonical_truth is None


def test_queue_is_sorted_by_project_and_requirement():
    later = build_trace(
        "REQ-B",
        "B",
        "lifeai",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/b.md"),)},
    )
    earlier = build_trace(
        "REQ-A",
        "A",
        "lifeai",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/a.md"),)},
    )
    other_project = build_trace(
        "REQ-Z",
        "Z",
        "project-a",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/z.md"),)},
    )

    assert [t.requirement_id for t in forgotten_requirements([later, other_project, earlier])] == [
        "REQ-A",
        "REQ-B",
        "REQ-Z",
    ]
