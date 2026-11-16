"""MainAI Research -- `app.mainai_research.book_provenance` -- proves claim lineage traces real
research-ledger rows (never fabricated), correctly collapses repeated-source citations, and the
founder-facing Research Council Output always carries `still_unknown`/`what_would_change_our_
mind` even when empty (never silently omitted) and never fabricates certainty.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_research.book_provenance import synthesize_research_council_output, trace_claim_lineage
from app.mainai_research.research_ledger import record_evidence, record_hypothesis, record_investigation
from app.mainai_research.types import EvidenceRole
from app.mainai_vision.evidence import EvidenceState
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"bp-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def test_trace_claim_lineage_collapses_repeated_source(superuser_db, owner_id):
    investigation = record_investigation(superuser_db, owner_id=owner_id, question="Q?", idempotency_key=f"inv-{uuid.uuid4()}")
    hypothesis = record_hypothesis(superuser_db, owner_id=owner_id, investigation_id=investigation["id"], statement="H", idempotency_key=f"hyp-{uuid.uuid4()}")
    superuser_db.commit()
    for i in range(10):
        record_evidence(
            superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"], role=EvidenceRole.SUPPORT,
            underlying_source_id="wire-story-A", evidence_state=EvidenceState.OBSERVED, summary=f"citation {i}",
            idempotency_key=f"ev-{uuid.uuid4()}",
        )
    superuser_db.commit()

    lineage = trace_claim_lineage(superuser_db, owner_id=owner_id, hypothesis_id=hypothesis["id"])
    assert lineage.primary_vs_secondary_counts["total"] == 10
    assert lineage.independent_source_count == 1


def test_research_council_output_always_carries_unknowns_field_even_when_empty():
    output = synthesize_research_council_output(
        current_best_explanation="A caused X", confidence=0.8, independent_source_count=3, total_cited_count=3,
        falsification_rounds_survived=2,
    )
    assert output.still_unknown == ()
    assert output.what_would_change_our_mind == ()
    assert output.authorized is False
    assert "no collapse observed" in output.source_independence_summary


def test_research_council_output_discloses_source_collapse():
    output = synthesize_research_council_output(
        current_best_explanation="A caused X", confidence=0.4, independent_source_count=1, total_cited_count=10,
        falsification_rounds_survived=0,
    )
    assert "collapse" in output.source_independence_summary


def test_research_council_output_never_fabricates_confidence_when_none_supplied():
    output = synthesize_research_council_output(
        current_best_explanation="unclear yet", confidence=None, independent_source_count=0, total_cited_count=0,
        falsification_rounds_survived=0, still_unknown=("root cause",),
    )
    assert output.confidence is None
    assert "root cause" in output.still_unknown
