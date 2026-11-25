"""Phase 3 acceptance: traceability gaps and disagreement without false truth."""

from __future__ import annotations

from app.context_atlas.traceability import (
    ContextClass,
    EvidenceRef,
    LinkStatus,
    SourceStatement,
    Stage,
    build_trace,
    reconcile_disagreement,
)


def _full_chain() -> dict:
    return {
        Stage.vision: (EvidenceRef("doc", "docs/vision.md", True),),
        Stage.requirement: (EvidenceRef("doc", "docs/requirements.md", True),),
        Stage.decision: (EvidenceRef("founder_decision", "decision:2026-11-20", True),),
        Stage.architecture: (EvidenceRef("doc", "docs/architecture.md", True),),
        Stage.implementation: (EvidenceRef("code", "backend/app/example.py", True),),
        Stage.test: (EvidenceRef("test", "tests/backend/test_example.py::test_x", True),),
        Stage.review: (EvidenceRef("pull_request", "PR #42", True),),
        Stage.integration: (EvidenceRef("code", "integration/base@abc123", True),),
        Stage.runtime_evidence: (EvidenceRef("runtime", "probe:2026-11-24", True),),
    }


def test_full_chain_is_verified_and_still_not_canonical_truth():
    trace = build_trace("REQ-1", "Example", "lifeai", evidence=_full_chain())

    assert trace.classification is ContextClass.verified_capability
    assert trace.missing_stages == ()
    assert trace.uncertainty == "low"
    assert trace.canonical_truth is None
    assert trace.execution_authorized is False
    assert all(link.status is LinkStatus.evidenced for link in trace.links)


def test_missing_stages_are_named_not_invented():
    trace = build_trace(
        "REQ-2",
        "Partly built",
        "lifeai",
        evidence={
            Stage.implementation: (EvidenceRef("code", "backend/app/partly.py"),),
            Stage.test: (EvidenceRef("test", "tests/backend/test_partly.py"),),
        },
    )

    assert trace.classification is ContextClass.implemented_capability
    assert Stage.vision in trace.missing_stages
    assert Stage.review in trace.missing_stages
    assert Stage.runtime_evidence in trace.missing_stages
    assert trace.uncertainty == "medium"


def test_vision_only_is_a_historical_suggestion():
    trace = build_trace(
        "REQ-3",
        "Old idea",
        "lifeai",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/old-idea.md"),)},
    )

    assert trace.classification is ContextClass.historical_suggestion
    assert trace.uncertainty == "high"
    assert trace.canonical_truth is None


def test_rejected_and_superseded_are_not_revived():
    rejected = build_trace(
        "REQ-4",
        "Rejected approach",
        "lifeai",
        evidence=_full_chain(),
        rejected=True,
    )
    superseded = build_trace(
        "REQ-5",
        "Old decision",
        "lifeai",
        evidence=_full_chain(),
        superseded_by="decision:2026-11-24",
    )

    assert rejected.classification is ContextClass.rejected_approach
    assert superseded.classification is ContextClass.superseded_decision
    assert superseded.superseded_by == "decision:2026-11-24"


def test_open_question_is_not_a_decision():
    trace = build_trace(
        "REQ-6",
        "Open question",
        "lifeai",
        evidence={Stage.requirement: (EvidenceRef("doc", "docs/question.md"),)},
        open_question=True,
    )
    assert trace.classification is ContextClass.open_question


def test_disagreement_preserves_both_statements():
    a = SourceStatement("doc-a", "The stack is Next.js 14.", "2026-01-01", "lifeai", "historical")
    b = SourceStatement("doc-b", "The stack is Next.js 16.", "2026-11-20", "lifeai", "active")

    disagreement = reconcile_disagreement(a, b)

    assert disagreement.a.statement == a.statement
    assert disagreement.b.statement == b.statement
    assert disagreement.project_scope == "same_project"
    assert disagreement.uncertainty == "medium"
    assert disagreement.possible_supersession is not None
    assert "doc-b" in disagreement.possible_supersession
    assert disagreement.canonical_truth is None
    assert disagreement.execution_authorized is False


def test_different_projects_are_not_compared_as_truth():
    a = SourceStatement("doc-a", "Uses SQLite.", None, "project-a")
    b = SourceStatement("doc-b", "Uses Postgres.", None, "project-b")

    disagreement = reconcile_disagreement(a, b)

    assert disagreement.project_scope == "different_projects"
    assert disagreement.uncertainty == "high"
    assert disagreement.possible_supersession is None
    assert disagreement.canonical_truth is None


def test_explicit_supersession_evidence_is_only_a_candidate():
    a = SourceStatement("doc-a", "Feature is disabled.", "2026-11-01", "lifeai")
    b = SourceStatement("doc-b", "Feature is enabled.", "2026-11-02", "lifeai")

    disagreement = reconcile_disagreement(a, b, supersession_evidence="PR #77 merged")

    assert disagreement.possible_supersession == "PR #77 merged"
    assert disagreement.canonical_truth is None
    assert any("review required" in note for note in disagreement.notes)
