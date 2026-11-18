"""M6 tests (Round 10): SCIENTIFIC REASONING + the BOUNDED INVESTIGATION POLICY.

Proven on real PostgreSQL with real stamped metadata and declared edges:

  1. The composition axes count what the founder actually declared: long/short follow-up,
     cumulative exposure, dose-response documentation, declared interactions, population
     spread — each from the closed-vocabulary metadata, never inferred.
  2. The confounding gap: causal claims without declared alternatives are COUNTED and
     named — the number the reviewer and the prompt layer both consume.
  3. The missing-evidence report: closed-vocabulary axes never declared for the topic
     are listed — absence is REPORTED, not inferred away.
  4. The bounded investigation policy hits all four outcomes: answer (sufficient),
     investigate (declared disagreement / funding flag / confounding gap — with a COST
     BOUND), stop (conflict resolved / normative question), report_uncertainty
     (< 2 studies). Depth is capped, never open-ended.
  5. The production ride: the science + policy line appears in the CHAT prompt for a
     retrieved topic — deterministic text, model-independent.
  6. Owner scoping end to end.
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
from app.rag.scientific_reasoning import (
    analyze_topic_science,
    format_topic_science_instructions,
    investigation_policy,
)
from app.request_context import current_user_id as current_user_id_var
from tests.backend.test_claim_graph_integration import _login


def get_dim():
    from app.config import get_settings

    return get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _study(db, owner_id, *, topic, meta):
    _set_rls_user(db, owner_id)
    source = Document(title=f"Sci {uuid.uuid4().hex[:6]}", source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    text = meta.pop("_text", f"Studieuttalande {uuid.uuid4().hex[:8]}.")
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=text, embedding=[0.1] * get_dim())
    db.add(chunk)
    db.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
        confidence=ClaimConfidence.likely, claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    full = {"research_topic": topic, **meta}
    record_claim_evidence_metadata(db, owner_id, claim.id, full, declared_via="sci_test")
    db.commit()
    return source, claim


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def test_composition_axes_count_declared_metadata(db_session, owner_id):
    t = "sci_kaffe"
    _study(db_session, owner_id, topic=t, meta={"study_design": "randomized_trial", "outcome_followup": "long_term", "exposure_duration": "chronic", "dose_response_documented": True, "dose": "3 koppar/dag", "interactions": ["koffein"], "population": "vuxna Sverige", "sample_size": 200})
    _study(db_session, owner_id, topic=t, meta={"study_design": "prospective_cohort", "outcome_followup": "short_term", "population": "äldre Japan", "sample_size": 1200})
    _study(db_session, owner_id, topic=t, meta={"study_design": "in_vitro", "outcome_followup": "days", "_text": "Mekanistisk studie om kaffe."})

    science = analyze_topic_science(db_session, owner_id, t)
    assert science.study_count == 3
    # "days" (the in_vitro study) is genuinely short-term — 2 short + 1 long.
    assert science.long_term_followup == 1 and science.short_term_followup == 2
    assert science.cumulative_exposure_studies == 1
    assert science.dose_response_documented == 1
    assert science.dose_range_reported == ["3 koppar/dag"]
    assert science.interaction_studies == 1
    assert science.distinct_populations == 2
    assert science.mechanistic_studies == 1 and science.experimental_studies == 1 and science.observational_studies == 1


def test_confounding_gap_is_counted(db_session, owner_id):
    t = "sci_conf"
    _study(db_session, owner_id, topic=t, meta={"study_design": "case_control", "causal_claim": True, "causal_claim_supported_by_design": False})
    _study(db_session, owner_id, topic=t, meta={"study_design": "randomized_trial", "causal_claim": True, "causal_claim_supported_by_design": True, "competing_explanations": ["controlled"]})

    science = analyze_topic_science(db_session, owner_id, t)
    assert science.causal_claims_total == 2
    assert science.causal_without_alternatives == 1, "the observational causal claim without declared alternatives is the gap"


def test_missing_evidence_axes_are_reported(db_session, owner_id):
    t = "sci_missing"
    _study(db_session, owner_id, topic=t, meta={"study_design": "randomized_trial"})

    science = analyze_topic_science(db_session, owner_id, t)
    assert "sample_size" in science.unknown_axes and "competing_explanations" in science.unknown_axes
    assert "study_design" not in science.unknown_axes, "declared axes are not reported missing"

    # A fully stamped topic reports NO missing load-bearing axes.
    _study(db_session, owner_id, topic="sci_complete", meta={"study_design": "randomized_trial", "sample_size": 100, "competing_explanations": ["controlled"], "causal_claim": False})
    assert analyze_topic_science(db_session, owner_id, "sci_complete").unknown_axes == []


def test_policy_hits_all_four_outcomes(db_session, owner_id):
    t_ok = "sci_policy_ok"
    _study(db_session, owner_id, topic=t_ok, meta={"study_design": "randomized_trial", "sample_size": 300, "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True})
    _study(db_session, owner_id, topic=t_ok, meta={"study_design": "prospective_cohort", "sample_size": 900, "population": "vuxna", "competing_explanations": ["controlled"], "causal_claim": False})

    science = analyze_topic_science(db_session, owner_id, t_ok)
    policy = investigation_policy(science)
    assert policy.policy == "answer" and policy.max_studies_to_examine <= 2

    # A declared contradiction flips the policy to investigate WITH a cost bound.
    _s1, claim_a = _study(db_session, owner_id, topic="sci_policy_conf", meta={"study_design": "randomized_trial", "sample_size": 100, "competing_explanations": ["controlled"]})
    _s2, claim_b = _study(db_session, owner_id, topic="sci_policy_conf", meta={"study_design": "prospective_cohort", "sample_size": 500, "competing_explanations": ["controlled"]})
    _set_rls_user(db_session, owner_id)
    db_session.add(ClaimRelationship(owner_id=owner_id, from_claim_id=claim_a.id, to_claim_id=claim_b.id, relationship_type=ClaimRelationshipType.contradicts))
    db_session.commit()
    science_conf = analyze_topic_science(db_session, owner_id, "sci_policy_conf")
    policy_conf = investigation_policy(science_conf)
    assert policy_conf.policy == "investigate"
    assert 4 <= policy_conf.max_studies_to_examine <= 10, "the investigation depth is CAPPED"

    # Conflict resolved -> stop (re-litigating a reflected conflict adds nothing).
    resolved = investigation_policy(science_conf, conflict_resolved=True)
    assert resolved.policy in ("stop", "answer")

    # Normative questions stop immediately.
    assert investigation_policy(science_conf, normative_question=True).policy == "stop"

    # < 2 studies -> report uncertainty, never fabricated certainty.
    lonely = analyze_topic_science(db_session, owner_id, "sci_missing")
    assert investigation_policy(lonely).policy == "report_uncertainty"


def test_science_and_policy_ride_the_chat_prompt(db_session, client, monkeypatch):
    """The production ride: the composition + policy line appears in the assembled chat
    prompt for a retrieved topic — deterministic, model-independent."""
    from app.models.user import User
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    captured: dict = {}

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * get_dim() for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(content="Svar.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _study(db_session, founder.id, topic="sci_prompt", meta={"study_design": "randomized_trial", "sample_size": 100, "competing_explanations": ["controlled"]})
    _study(db_session, founder.id, topic="sci_prompt", meta={"study_design": "in_vitro", "_text": "Kaffe och puls mekanistik."})
    csrf = _login(client)
    res = client.post("/api/chat", json={"conversation_id": None, "message": "Vad vet vi om kaffe och puls?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    system = captured["system"]
    assert "VETENSKAPLIG SAMMANSÄTTNING 'sci_prompt'" in system
    assert "UNDERSÖKNINGSPOLICY" in system


def test_topic_science_owner_scoped(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _study(db_session, owner_a.id, topic="sci_iso", meta={"study_design": "randomized_trial", "sample_size": 100})
    _set_rls_user(db_session, owner_b.id)
    science_b = analyze_topic_science(db_session, owner_b.id, "sci_iso")
    assert science_b.study_count == 0
    assert investigation_policy(science_b).policy == "report_uncertainty"
