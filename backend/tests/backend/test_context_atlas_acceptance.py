"""Phase 5 acceptance cases not already covered by the focused suites.

Cases 1-5, 7, 10-12, 14-15 live in test_context_atlas_identity.py,
test_context_atlas_candidates.py, test_context_atlas_reconciliation.py,
test_context_atlas_traceability.py, test_context_atlas_system_map.py and
test_context_atlas_recommendation.py. This file adds 6, 8, 9, 13 and 16 and proves they run
against the real checkout rather than a mock.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.context_atlas.identity import compare_sources
from app.context_atlas.recommendation import (
    CapabilityFact,
    EngineeringRequest,
    RecommendationOutcome,
    recommend,
)
from app.context_atlas.reconciliation import ContextObservation, Outcome, reconcile
from app.context_atlas.system_map import scan_system_map
from app.context_atlas.traceability import (
    ContextClass,
    EvidenceRef,
    Stage,
    build_trace,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "context-atlas"

# Shapes that would indicate a real credential leaked into a generated artifact. Built from
# split literals so this detector cannot match its own source (and so the export scanner does
# not flag the atlas branch that contains this file).
_GITHUB_PAT = "github_" + "pat_"
_XOX = "xox" + "[baprs]-"
SECRET_SHAPES = re.compile(
    r"AKIA[0-9A-Z]{16}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|"
    r"ghp_[A-Za-z0-9]{36}|" + _GITHUB_PAT + r"[A-Za-z0-9_]{22,}|"
    r"sk-[A-Za-z0-9]{32,}|" + _XOX + r"[A-Za-z0-9-]{10,}"
)


def _facts(system) -> dict[str, CapabilityFact]:
    return {
        node.module: CapabilityFact(
            component=node.module,
            present=node.maturity.present,
            wired=node.maturity.wired,
            test_referenced=node.maturity.test_referenced,
            path=node.path,
            endpoints=node.endpoints,
        )
        for node in system.modules
    }


def test_case_4_rewritten_document_is_related_not_identical():
    original = (
        "LifeAI stores founder knowledge with provenance, immutable originals and "
        "row level security. Retrieval is grounded in those sources."
    ).encode()
    rewritten = (
        "Founder knowledge inside LifeAI keeps provenance and immutable originals, "
        "protected by row level security, and retrieval stays grounded in them."
    ).encode()

    assessment = compare_sources(original, rewritten)

    assert assessment.verdict.value in {"similar", "near_duplicate"}
    assert assessment.authoritative is False
    assert assessment.proposed_relationship is None
    assert assessment.preserve_originals is True


def test_case_5_historical_context_is_assigned_to_its_project():
    trace = build_trace(
        "PROJ-A-1",
        "Historical project A decision",
        "project-a",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/project-a/vision.md"),)},
    )
    assert trace.project_ref == "project-a"
    assert trace.classification is ContextClass.historical_suggestion

    result = recommend(
        EngineeringRequest(
            "implement this in the current project",
            capability_aliases=("retrieval",),
            other_project=True,
        ),
        _facts(scan_system_map(REPO_ROOT)),
    )
    assert result.outcome is RecommendationOutcome.ask


def test_case_6_misleading_historical_stack_is_not_the_current_stack():
    historical = build_trace(
        "HIST-STACK",
        "The system runs on the 2024 stack",
        "lifeai",
        evidence={Stage.vision: (EvidenceRef("doc", "docs/archive/2024-stack.md"),)},
    )

    assert historical.classification is ContextClass.historical_suggestion
    assert historical.classification is not ContextClass.verified_capability
    assert historical.canonical_truth is None
    assert Stage.implementation in historical.missing_stages


def test_case_8_existing_capability_is_read_from_real_code_evidence():
    system = scan_system_map(REPO_ROOT)
    facts = _facts(system)

    assert "app.rag" in facts
    assert facts["app.rag"].present is True
    assert facts["app.rag"].wired is True

    result = recommend(
        EngineeringRequest("build retrieval from scratch", capability_aliases=("retrieval",)),
        facts,
    )
    assert result.outcome is RecommendationOutcome.reuse
    assert any("app.rag" in item for item in result.evidence)


def test_case_9_unmerged_or_unobserved_is_never_called_deployed():
    system = scan_system_map(REPO_ROOT)
    chat = system.module("app.routers.chat")

    assert chat is not None
    assert chat.maturity.wired is True
    # A checkout cannot prove merge, activation or runtime observation.
    assert chat.maturity.integrated is None
    assert chat.maturity.activated is None
    assert chat.maturity.observed is None


def test_case_13_ambiguous_history_cannot_trigger_execution():
    decision = reconcile(
        ContextObservation(ambiguous=True, action_requested=True, founder_decision_required=True)
    )

    assert decision.outcome is Outcome.ask
    assert decision.execution_authorized is False
    assert "do not start work" in decision.next_step


def test_case_16_no_credentials_or_authorization_in_generated_artifacts():
    payload = json.dumps(scan_system_map(REPO_ROOT).to_dict(), ensure_ascii=False)
    assert not SECRET_SHAPES.search(payload)
    assert '"execution_authorized": true' not in payload

    artifacts = sorted(EVIDENCE_DIR.glob("*.json"))
    assert artifacts, "expected committed evidence artifacts"
    for path in artifacts:
        text = path.read_text(encoding="utf-8")
        assert not SECRET_SHAPES.search(text), f"credential-like value in {path.name}"
        assert '"execution_authorized": true' not in text, f"authorization claim in {path.name}"
