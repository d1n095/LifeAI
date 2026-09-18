"""`app.mainai_coverage.dynamic_denominator` -- proves a valid omission finding really stages a
new implied requirement via the EXISTING, unchanged `mainai_vision.gap_generator` pipeline
(staging only, never promotion). See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import uuid

from app.mainai_coverage.dynamic_denominator import stage_omission_for_vision_expansion
from app.mainai_coverage.omission_discovery import OmissionFinding, find_omissions
from app.mainai_coverage.types import CapabilityClaim
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User


def test_valid_omission_expands_the_canonical_vision_denominator(superuser_db):
    owner = User(email=f"cov-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    document = Document(title="Source", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text="autonomous development", extraction_version="v1")
    superuser_db.add(claim)
    superuser_db.flush()
    superuser_db.commit()

    capability_claim = CapabilityClaim(claim_id="c1", description="autonomous development", mention_count=4)
    findings = find_omissions(claims=(capability_claim,), canonical_vision_texts=frozenset())
    assert len(findings) == 1
    assert findings[0].recommend_denominator_expansion is True

    result = stage_omission_for_vision_expansion(
        superuser_db, owner_id=owner.id, source_claim_id=claim.id, finding=findings[0], capability_description="autonomous development",
    )
    superuser_db.commit()

    assert result is not None
    assert len(result.staged_proposal_ids) == len(result.gap_report.implied_requirements)
    assert len(result.staged_proposal_ids) > 0


def test_non_expansion_finding_stages_nothing(superuser_db):
    finding = OmissionFinding(claim_id="c2", kind="built_never_verified", reason="x", canonical_overlap_score=1.0, recommend_denominator_expansion=False)
    result = stage_omission_for_vision_expansion(
        superuser_db, owner_id=uuid.uuid4(), source_claim_id=uuid.uuid4(), finding=finding, capability_description="irrelevant",
    )
    assert result is None
