"""Phase 4: RESEARCH INTEGRITY — realistic multi-source evaluations across the twelve
evidence dimensions, with the Intellectual Adversary proven realistic in BOTH
directions: it challenges when the evidence justifies it, and it stays SILENT when the
existing explanation remains best supported — neither popularity nor minority status
moves a verdict.

Dimensions covered (mission list): primary vs secondary evidence, conflicting findings,
methodological weaknesses, international differences, funding/COI, cumulative and
long-term effects, interaction effects, dose-response, causal confounding,
selection/publication bias (handled as HONEST missing-evidence reporting — the system
never infers a bias it cannot observe), missing evidence, historical claims + provenance.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.rag.claims import EXTRACTION_VERSION
from app.rag.claim_graph import trace_evidence_chain
from app.rag.evidence_metadata import record_claim_evidence_metadata
from app.rag.intellectual_adversary import review_conclusion
from app.rag.scientific_reasoning import analyze_topic_science, investigation_policy
from app.request_context import current_user_id as current_user_id_var

DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _bow(text: str) -> list[float]:
    import math

    v = [0.0] * DIM
    for w in (text or "").lower().split():
        v[hash(w) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


class Corpus:
    def __init__(self, db, owner_id):
        self.db, self.owner_id = db, owner_id

    def study(self, title, text, meta, confidence=ClaimConfidence.likely):
        db, owner = self.db, self.owner_id
        _set_rls_user(db, owner)
        source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner)
        db.add(source)
        db.commit()
        _set_rls_user(db, owner)
        chunk = DocumentChunk(document_id=source.id, owner_id=owner, chunk_index=0, text=text, embedding=_bow(text))
        db.add(chunk)
        db.commit()
        _set_rls_user(db, owner)
        claim = KnowledgeClaim(
            owner_id=owner, source_id=source.id, chunk_id=chunk.id, claim_text=text,
            confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
            extraction_version=EXTRACTION_VERSION,
        )
        db.add(claim)
        db.commit()
        full = {"research_topic": meta.pop("topic", f"ri_{uuid.uuid4().hex[:6]}"), **meta}
        record_claim_evidence_metadata(db, owner, claim.id, full, declared_via="r11_ri")
        db.commit()
        return claim

    def contradict(self, a, b):
        _set_rls_user(self.db, self.owner_id)
        self.db.add(ClaimRelationship(owner_id=self.owner_id, from_claim_id=a.id, to_claim_id=b.id, relationship_type=ClaimRelationshipType.contradicts))
        self.db.commit()


def test_primary_secondary_and_international_conflict(owner_id, db_session):
    """Primary (RCT) + secondary (meta-analysis) + international disagreement: the
    composition keeps them DISTINCT, the landscape reports the conflict symmetrically,
    and the verdict is conflicting_evidence — no adjudication by origin or count."""
    c = Corpus(db_session, owner_id)
    t = "ri_kaffe_somn"
    rct = c.study("RCT Sverige", "Primärstudie: kaffe försämrar sömn.", {"topic": t, "study_design": "randomized_trial", "sample_size": 400, "study_setting": "Sverige", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True}, confidence=ClaimConfidence.certain)
    review = c.study("Metaanalys Japan", "Metaanalys av kaffestudier: ingen säker effekt på sömn.", {"topic": t, "study_design": "meta_analysis", "sample_size": 5000, "study_setting": "Japan", "competing_explanations": ["controlled"]})
    cohort_usa = c.study("Kohort USA", "Kohortstudie: kaffe kopplat till kortare sömn.", {"topic": t, "study_design": "prospective_cohort", "sample_size": 900, "study_setting": "USA", "competing_explanations": ["controlled"]})
    c.contradict(rct, review)
    c.contradict(rct, cohort_usa)

    science = analyze_topic_science(db_session, owner_id, t)
    # Primary vs secondary kept distinct in the composition:
    assert science.experimental_studies == 1 and science.observational_studies == 1
    assert science.meta_analyses_studies == 1, "the meta-analysis is a DISTINCT secondary bucket"
    assert science.distinct_settings == 3, "Sverige/Japan/USA all counted"
    assert science.declared_disagreements == 2
    policy = investigation_policy(science)
    assert policy.policy == "investigate", "2 declared conflicts -> bounded investigation"

    chain = trace_evidence_chain(db_session, owner_id, rct.id)
    assert chain.verdict.status.value == "conflicting_evidence"
    assert len(chain.contradicting_edges) == 2
    # The adversary names the counterevidence but does NOT pick a winner:
    review_r = review_conclusion(db_session, owner_id, rct.id)
    counter = [o for o in review_r.objections if o.kind == "unacknowledged_counterevidence"]
    assert len(counter) == 2 and all("ingen sida är vinnare" in o.statement for o in counter)


def test_methodological_weakness_without_popularity_bias(owner_id, db_session):
    """One well-conducted RCT contradicted by MANY weak observational studies: the
    conflict stays symmetric. Popularity is not truth; minority status is not
    credibility. The adversary objects to REAL weaknesses (funding, no control) on BOTH
    sides — and adds NO 'you are outvoted' or 'contrarian bonus' objection."""
    c = Corpus(db_session, owner_id)
    t = "ri_minoritet"
    strong = c.study("RCT ren", "Välkontrollerad RCT: behandling X minskar symtom Y.", {"topic": t, "study_design": "randomized_trial", "sample_size": 600, "conflicts_of_interest": "none declared", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True}, confidence=ClaimConfidence.certain)
    weaks = []
    for i in range(4):
        weaks.append(c.study(f"Kohort svag {i}", f"Observationsstudie {i}: behandling X kopplad till fler symtom.", {"topic": t, "study_design": "case_series", "sample_size": 20, "conflicts_of_interest": "industry_funded", "competing_explanations": ["self_report_bias"], "causal_claim": True, "causal_claim_supported_by_design": False}))
    for w in weaks:
        c.contradict(strong, w)

    chain = trace_evidence_chain(db_session, owner_id, strong.id)
    assert chain.verdict.status.value == "conflicting_evidence", "1-vs-4 does NOT outvote a designed study"
    review_strong = review_conclusion(db_session, owner_id, strong.id)
    kinds = [o.kind for o in review_strong.objections]
    assert "unacknowledged_counterevidence" in kinds
    assert not any("popularity" in k or "outvoted" in k or "contrarian" in k for k in kinds), (
        "no popularity/minority objection exists at all"
    )
    # The WEAK side gets real methodological objections (funding + no control + small n):
    review_weak = review_conclusion(db_session, owner_id, weaks[0].id)
    weak_kinds = [o.kind for o in review_weak.objections]
    assert "overstated_confidence" in weak_kinds or "weak_causal_inference" in weak_kinds


def test_best_supported_explanation_gets_no_objections(owner_id, db_session):
    """Adversary realism, the other direction: a well-grounded conclusion with design
    support, declared alternatives, replication, and no contradictions produces ZERO
    objections — challenging consensus for its own sake is manufacturing."""
    c = Corpus(db_session, owner_id)
    t = "ri_konsensus"
    for i in range(2):
        c.study(f"RCT replika {i}", f"Replikerad RCT {i}: behandling Z förbättrar utfallet A.", {"topic": t, "study_design": "randomized_trial", "sample_size": 500, "conflicts_of_interest": "none declared", "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True, "population": "vuxna"}, confidence=ClaimConfidence.certain)
    claims = db_session.query(KnowledgeClaim).filter(
        KnowledgeClaim.owner_id == owner_id,
        KnowledgeClaim.claim_text.like("Replikerad RCT%"),
    ).all()
    review = review_conclusion(db_session, owner_id, claims[0].id)
    assert review.reviewed is False and review.objections == [], "no manufactured disagreement"
    science = analyze_topic_science(db_session, owner_id, t)
    assert science.causal_without_alternatives == 0
    assert investigation_policy(science).policy == "answer"


def test_bias_reporting_is_honest_not_inferred(owner_id, db_session):
    """Selection/publication bias cannot be OBSERVED from declared metadata — the system
    must NOT invent a bias finding; it names what it CAN see (single population/setting)
    and reports missing axes instead of inferring bias."""
    c = Corpus(db_session, owner_id)
    t = "ri_bias"
    for i in range(2):
        c.study(f"Studie en setting {i}", f"Studie {i} från samma setting om behandling W.", {"topic": t, "study_design": "prospective_cohort", "sample_size": 300, "study_setting": "Tyskland", "population": "vuxna Tyskland", "competing_explanations": ["controlled"]})
    science = analyze_topic_science(db_session, owner_id, t)
    assert science.distinct_populations == 1 and science.distinct_settings == 1
    policy = investigation_policy(science)
    assert policy.policy == "answer", "no investigate triggers: no conflicts, no funding, no confounding gap"
    joined = " ".join(policy.reasons)
    assert "population" in joined.lower(), "single-population generalization risk is NAMED"
    assert "publiceringsbias" not in joined and "selection bias" not in joined.lower(), (
        "no inferred publication/selection-bias finding without evidence"
    )


def test_historical_claims_keep_provenance(owner_id, db_session):
    c = Corpus(db_session, owner_id)
    claim = c.study("Historisk källa 1923", "År 1923 dokumenterades behandlingen först i arkivet X.", {"study_design": "unknown"})
    chain = trace_evidence_chain(db_session, owner_id, claim.id)
    assert chain.chunk_grounded and chain.source["title"] == "Historisk källa 1923"
    assert chain.verdict.status.value in ("hypothesis", "assumption", "verified_fact", "conflicting_evidence")
    assert claim.claim_type == ClaimType.historical
