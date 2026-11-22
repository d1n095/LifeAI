"""M2 tests (Round 9): the TOPIC layer of evidence intelligence — per-topic landscapes,
declared disagreements lifted from the claim graph, cross-country/cross-design flags, and
the methodology range. Everything stays provenance-aware and source-type-neutral.

What is proven on real PostgreSQL through real stamped metadata and real claim-graph
edges:
  1. Topic scoping: studies of one topic aggregate; a second topic's studies never leak
     into the first topic's landscape.
  2. Disagreement detection is DERIVED, never inferred: only a founder-declared contradicts
     edge between two same-topic claims produces a disagreement — text similarity, same
     design, or opposite wording alone produce NONE (no fabricated conflict, and no
     fabricated consensus either: zero declared conflicts is reported as such, explicitly
     NOT as agreement).
  3. Cross-country and cross-design disagreement flags are derived from the two sides'
     DECLARED study_setting / study_design — reported symmetrically, no winner.
  4. Methodology range: the topic's declared sample sizes and design classes are reported
     as a range, so an n=12-vs-n=1200 methodological gap is visible.
  5. The instruction line carries the neutrality rules in its own text.
  6. Owner isolation: another owner's same-topic studies are invisible.
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
from app.rag.evidence_metadata import record_claim_evidence_metadata, summarize_topic_landscape
from app.rag.evidence_metadata import format_topic_landscape_instructions
from app.request_context import current_user_id as current_user_id_var

DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _topic_study(db, owner_id, *, topic, setting=None, design="randomized_trial", n=200, text=None):
    _set_rls_user(db, owner_id)
    source = Document(title=f"Studie {uuid.uuid4().hex[:6]}", source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    claim_text = text or f"Studieuttalande {uuid.uuid4().hex[:8]}."
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=claim_text, embedding=[0.1] * DIM)
    db.add(chunk)
    db.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=claim_text,
        confidence=ClaimConfidence.likely, claim_type=ClaimType.historical,
        status=ClaimStatus.active, extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    meta = {"research_topic": topic, "study_design": design, "sample_size": n}
    if setting:
        meta["study_setting"] = setting
    record_claim_evidence_metadata(db, owner_id, claim.id, meta, declared_via="topic_test")
    db.commit()
    return source, claim


# --- 1. scoping ---------------------------------------------------------------------------------


def test_topic_scoping_includes_own_and_excludes_other_topics(db_session, owner_id):
    _s1, kaffe_a = _topic_study(db_session, owner_id, topic="kaffe_sömn", setting="Sverige")
    _s2, kaffe_b = _topic_study(db_session, owner_id, topic="kaffe_sömn", setting="Japan")
    _s3, alcohol = _topic_study(db_session, owner_id, topic="alkohol_livslängd")

    landscape = summarize_topic_landscape(db_session, owner_id, "kaffe_sömn")
    assert landscape.base.study_count == 2, "only the topic's own studies aggregate"
    assert landscape.base.settings_reported == ["Japan", "Sverige"]
    assert landscape.disagreements == []

    other = summarize_topic_landscape(db_session, owner_id, "alkohol_livslängd")
    assert other.base.study_count == 1

    empty = summarize_topic_landscape(db_session, owner_id, "okänt_ämne")
    assert empty.base.study_count == 0
    assert "OLÖST" in format_topic_landscape_instructions(empty), "an unstudied topic reads as UNRESOLVED"


# --- 2. disagreements are derived, never inferred ------------------------------------------------


def test_disagreement_requires_a_declared_edge(db_session, owner_id):
    # Two same-topic claims with OPPOSITE wording — no edge declared: NO disagreement.
    _s1, pro = _topic_study(db_session, owner_id, topic="kaffe_blodtryck", setting="Sverige", text="Kaffe höjer blodtrycket.")
    _s2, con = _topic_study(db_session, owner_id, topic="kaffe_blodtryck", setting="Japan", text="Kaffe sänker blodtrycket.")

    landscape = summarize_topic_landscape(db_session, owner_id, "kaffe_blodtryck")
    assert landscape.disagreements == [], (
        "opposite wording without a declared contradicts edge is NOT a disagreement — "
        "the system never fabricates a conflict from text similarity"
    )
    assert "inga deklarerade motsägelser (avsaknad av konflikt är inte konsensus)" in (
        format_topic_landscape_instructions(landscape)
    ), "zero conflicts is explicitly NOT consensus"

    # The founder DECLARES the contradiction — now it exists, with provenance.
    db_session.add(
        ClaimRelationship(
            owner_id=owner_id, from_claim_id=pro.id, to_claim_id=con.id,
            relationship_type=ClaimRelationshipType.contradicts,
        )
    )
    db_session.commit()
    landscape2 = summarize_topic_landscape(db_session, owner_id, "kaffe_blodtryck")
    assert len(landscape2.disagreements) == 1
    d = landscape2.disagreements[0]
    assert {d.from_claim_text, d.to_claim_text} == {"Kaffe höjer blodtrycket.", "Kaffe sänker blodtrycket."}
    assert d.from_setting == "Sverige" and d.to_setting == "Japan"
    assert d.cross_country is True


def test_cross_country_and_cross_design_flags_are_symmetric_metadata(db_session, owner_id):
    # Same country, different design -> cross_design only.
    _s1, a = _topic_study(db_session, owner_id, topic="sömn_längd", setting="Tyskland", design="randomized_trial")
    _s2, b = _topic_study(db_session, owner_id, topic="sömn_längd", setting="Tyskland", design="prospective_cohort")
    # Different countries, same design -> cross_country only.
    _s3, c = _topic_study(db_session, owner_id, topic="sömn_kvalitet", setting="Brasilien", design="randomized_trial")
    _s4, d = _topic_study(db_session, owner_id, topic="sömn_kvalitet", setting="Kenya", design="randomized_trial")

    db_session.add_all(
        [
            ClaimRelationship(owner_id=owner_id, from_claim_id=a.id, to_claim_id=b.id, relationship_type=ClaimRelationshipType.contradicts),
            ClaimRelationship(owner_id=owner_id, from_claim_id=c.id, to_claim_id=d.id, relationship_type=ClaimRelationshipType.contradicts),
        ]
    )
    db_session.commit()

    sleep_l = summarize_topic_landscape(db_session, owner_id, "sömn_längd")
    assert sleep_l.cross_design_disagreements == 1 and sleep_l.cross_country_disagreements == 0
    assert sleep_l.disagreements[0].from_design == "randomized_trial" and sleep_l.disagreements[0].to_design == "prospective_cohort"

    sleep_q = summarize_topic_landscape(db_session, owner_id, "sömn_kvalitet")
    assert sleep_q.cross_country_disagreements == 1 and sleep_q.cross_design_disagreements == 0

    line = format_topic_landscape_instructions(sleep_q)
    assert "länder emellan 1" in line and "välj INGEN vinnare" in line, (
        "the cross-country signal travels with its own no-winner rule"
    )


def test_methodology_range_surfaces_the_n_gap(db_session, owner_id):
    _s1, _a = _topic_study(db_session, owner_id, topic="doseringsfråga", design="randomized_trial", n=12)
    _s2, _b = _topic_study(db_session, owner_id, topic="doseringsfråga", design="prospective_cohort", n=1200)

    landscape = summarize_topic_landscape(db_session, owner_id, "doseringsfråga")
    assert landscape.sample_sizes_reported == [12, 1200]
    assert landscape.designs_reported == ["prospective_cohort", "randomized_trial"]
    line = format_topic_landscape_instructions(landscape)
    assert "n 12–1200" in line, "the methodological gap between studies is visible in the instruction"


def test_topic_landscape_owner_isolation(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _topic_study(db_session, owner_a.id, topic="gemensam_fråga", setting="Sverige")

    _set_rls_user(db_session, owner_b.id)
    landscape_b = summarize_topic_landscape(db_session, owner_b.id, "gemensam_fråga")
    assert landscape_b.base.study_count == 0, "A's topic evidence never reaches B's landscape"
    assert "OLÖST" in format_topic_landscape_instructions(landscape_b)
