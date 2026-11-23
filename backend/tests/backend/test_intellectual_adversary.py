"""M4 tests (Round 10): the INTELLECTUAL ADVERSARY — bounded, evidence-driven review.

Proven on real PostgreSQL through the production evidence-chain endpoint:

  1. NO automatic disagreement: a clean, well-grounded claim with declared design
     support and competing explanations produces reviewed=False — a straightforward
     correct answer is NOT degraded with manufactured objections.
  2. Every objection cites CONCRETE evidence (refs into the owner's own records) —
     the reviewer cannot invent sources or manufacture objections: an objection IS a
     provenance pointer, by construction.
  3. Weak causal inference + confounding fire together for an observational causal
     claim with no declared alternatives.
  4. Unacknowledged counterevidence fires per declared contradicts edge — named
     symmetrically, no winner.
  5. Overstated confidence fires when a low quality grade sits under a confident
     verdict (funding conflict floors the grade; the ladder verdict stays).
  6. Inconsistent conclusions fire from real verdict swings on the audit trail.
  7. Fabricated-citation guard: a claim without chunk grounding is flagged — the
     citation chain is broken.
  8. Epistemic separation: every objection labels its category, and the categories
     distinguish observed evidence / source claims / hypotheses / model inferences /
     verified conclusions / unresolved questions.
  9. The review rides the production endpoint (fail-open, owner-scoped).
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import record_claim_evidence_metadata
from app.rag.intellectual_adversary import review_conclusion
from app.request_context import current_user_id as current_user_id_var
from tests.backend.test_claim_graph_integration import _login

FOUNDER_EMAIL = "founder@lifeos.local"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _claim(db, owner_id, *, text, confidence=ClaimConfidence.likely):
    _set_rls_user(db, owner_id)
    source = Document(title=f"Adv {uuid.uuid4().hex[:6]}", source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=text, embedding=[0.1] * get_dim())
    db.add(chunk)
    db.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
        confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    return source, claim


def get_dim():
    from app.config import get_settings

    return get_settings().embedding_dim


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def test_clean_supported_conclusion_gets_no_objections(db_session, owner_id):
    """The reviewer must NOT disagree for its own sake: a well-grounded claim with
    declared design support and declared competing explanations yields reviewed=False."""
    _s, claim = _claim(db_session, owner_id, text="RCT-stödd slutsats om sömn och minne.")
    record_claim_evidence_metadata(
        db_session, owner_id, claim.id,
        {"study_design": "randomized_trial", "sample_size": 800, "study_setting": "Sverige",
         "conflicts_of_interest": "none declared", "causal_claim": True,
         "causal_claim_supported_by_design": True, "competing_explanations": ["controlled"]},
        declared_via="adv_test",
    )
    db_session.commit()
    review = review_conclusion(db_session, owner_id, claim.id)
    assert review.reviewed is False, "no manufactured objections"
    assert review.objections == []


def test_every_objection_cites_concrete_evidence(db_session, owner_id):
    from app.rag.intellectual_adversary import AdversarialObjection

    _s, claim = _claim(db_session, owner_id, text="Observationell kausal claim utan deklarerade alternativ.")
    record_claim_evidence_metadata(
        db_session, owner_id, claim.id,
        {"study_design": "prospective_cohort", "causal_claim": True, "causal_claim_supported_by_design": False},
        declared_via="adv_test",
    )
    db_session.commit()
    review = review_conclusion(db_session, owner_id, claim.id)
    assert review.reviewed is True
    kinds = {o.kind for o in review.objections}
    assert {"unsupported_assumption", "weak_causal_inference", "confounding_variables"} <= kinds
    for o in review.objections:
        assert isinstance(o, AdversarialObjection)
        assert o.evidence_refs and all(ref for ref in o.evidence_refs), "an objection IS a provenance pointer"
        assert o.epistemic_category in {"observed_evidence", "source_claim", "hypothesis", "model_inference", "verified_conclusion", "unresolved_question"}


def test_counterevidence_named_symmetrically(db_session, owner_id):
    _s1, a = _claim(db_session, owner_id, text="Påstående A adv.")
    _s2, b = _claim(db_session, owner_id, text="Påstående B adv.")
    db_session.add(ClaimRelationship(owner_id=owner_id, from_claim_id=b.id, to_claim_id=a.id, relationship_type=ClaimRelationshipType.contradicts))
    db_session.commit()
    review = review_conclusion(db_session, owner_id, a.id)
    counter = [o for o in review.objections if o.kind == "unacknowledged_counterevidence"]
    assert len(counter) == 1
    assert str(b.id) in counter[0].evidence_refs[2], "the counter-claim is named in the provenance"
    assert "ingen sida är vinnare" in counter[0].statement


def test_overstated_confidence_from_low_grade(db_session, owner_id):
    _s, claim = _claim(db_session, owner_id, text="Industrifinansierad RCT-stödd slutsats adv.")
    record_claim_evidence_metadata(
        db_session, owner_id, claim.id,
        {"study_design": "randomized_trial", "sample_size": 500, "conflicts_of_interest": "industry_funded",
         "causal_claim": True, "causal_claim_supported_by_design": True, "competing_explanations": ["controlled"],
         "research_topic": "adv_over"},
        declared_via="adv_test",
    )
    db_session.commit()
    review = review_conclusion(db_session, owner_id, claim.id)
    overstated = [o for o in review.objections if o.kind == "overstated_confidence"]
    assert overstated, "low grade under a confident verdict must be flagged"
    assert "low" in overstated[0].evidence_refs[1]


def test_inconsistent_conclusions_from_audit_swings(db_session, owner_id):
    _s, claim_a = _claim(db_session, owner_id, text="Påstående med växlande verdikt adv.")
    _s2, claim_b = _claim(db_session, owner_id, text="Motsägare adv.")
    from app.models.audit import AuditLog

    # TWO swings are needed (the reviewer flags repeated changes, not a single legitimate
    # revision); the detail format matches shifts_to_audit_detail's real output.
    for _i in range(2):
        db_session.add(
            AuditLog(
                user_id=owner_id, action="epistemic_shift", entity_type="claim_relationship",
                entity_id=str(uuid.uuid4()),
                detail="change on claim '" + str(claim_a.id) + "': 'verified_fact -> conflicting_evidence'",
            )
        )
    db_session.commit()
    review = review_conclusion(db_session, owner_id, claim_a.id)
    inconsistencies = [o for o in review.objections if o.kind == "inconsistent_conclusions"]
    assert inconsistencies, "real verdict swings on the audit trail are reviewable material"


def test_broken_citation_chain_flagged(db_session, owner_id):
    _s, claim = _claim(db_session, owner_id, text="Utggrundat påstående adv.")
    claim.chunk_id = None  # simulate a broken grounding (detached claim)
    db_session.commit()
    review = review_conclusion(db_session, owner_id, claim.id)
    fabricated = [o for o in review.objections if o.kind == "fabricated_citation"]
    assert fabricated, "a claim without chunk grounding cannot be verified against a document"
    assert fabricated[0].severity == "high"


def test_review_rides_the_production_endpoint(client, db_session, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _s, claim = _claim(db_session, founder.id, text="Endpoint adv påstående med kausal lucka.")
    record_claim_evidence_metadata(
        db_session, founder.id, claim.id,
        {"study_design": "case_control", "causal_claim": True, "causal_claim_supported_by_design": False},
        declared_via="adv_test",
    )
    db_session.commit()
    csrf = _login(client)
    res = client.get(f"/api/library/claims/{claim.id}/evidence-chain", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    body = res.json()
    review = body["adversarial_review"]
    assert review["reviewed"] is True
    kinds = {o["kind"] for o in review["objections"]}
    assert "weak_causal_inference" in kinds
    assert all(o["evidence_refs"] for o in review["objections"])


def test_review_owner_scoped(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _s, claim = _claim(db_session, owner_a.id, text="A:s kausal lucka adv.")
    record_claim_evidence_metadata(
        db_session, owner_a.id, claim.id, {"study_design": "case_control", "causal_claim": True}, declared_via="adv_test"
    )
    db_session.commit()
    _set_rls_user(db_session, owner_b.id)
    with pytest.raises(LookupError):
        review_conclusion(db_session, owner_b.id, claim.id)
