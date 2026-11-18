"""M3 tests (Round 9): the claim graph actually INFLUENCES chat and workbench outputs —
verified by capturing the system prompt the provider receives and proving it CHANGES when
graph state changes. An imported function or registered endpoint is not proof of a
functioning capability; the proof here is the delta in what the model is actually told.

Scenario matrix:
  1. No graph state            -> GRAFKONTEXT says so (explicit, never silent).
  2. A contradicts edge on a   -> the second-order consequence to the DEPENDENT report
     source another report       appears in the prompt, with its kind and depth.
     depends on
  3. The same statement in 2+  -> the question-relevant cross-domain overlap line appears
     projects + a question       (token-overlap >= 2 makes it RELEVANT, not a dump); an
     sharing its words           unrelated question produces NO overlap line.
  4. A stamped research_topic  -> the topic landscape line appears, and declaring a
     among the retrieved claims  contradicts edge inside the topic adds BOTH sides of the
                                 conflict, named, no winner.
  5. Owner isolation           -> another owner's identical graph setup never reaches the
                                 founder's prompt.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.project import Project
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import record_claim_evidence_metadata
from app.request_context import current_user_id as current_user_id_var
from tests.backend.test_claim_graph_integration import _login

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _doc_with_claim(db, owner_id, *, title, claim_text, project_id=None):
    _set_rls_user(db, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id, project_id=project_id)
    db.add(source)
    db.commit()
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
    return source, claim


@pytest.fixture
def prompt_capture(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    captured: dict = {}

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(content="Svar.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    return captured


def _ask(client, question, path="/api/chat", **extra):
    csrf = _login(client)
    if path == "/api/chat":
        res = client.post(path, json={"conversation_id": None, "message": question}, headers={"X-CSRF-Token": csrf})
    else:
        res = client.post(path, json={"question": question, **extra}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    return res


# --- 1-2: consequences reach the prompt and CHANGE with graph state -------------------------------


def test_chat_prompt_shows_graph_state_and_changes_with_a_contradiction(db_session, client, prompt_capture):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    root, _claim = _doc_with_claim(db_session, founder.id, title="Rotstudie", claim_text="X höjer markören Y.")
    csrf = _login(client)

    _ask(client, "Vad säger rotstudien?")
    assert "GRAFKONTEXT" in prompt_capture["system"], "the block exists even without graph state"
    assert "inga andrangordningseffekter" in prompt_capture["system"], "no graph state is stated EXPLICITLY"

    # A report depends on the root study; the root study gets contradicted -> the dependent
    # report stands on contested ground, and the prompt must say so.
    report, _r = _doc_with_claim(db_session, founder.id, title="Rapport bygger på rotstudien", claim_text="Sammanfattning.")
    _set_rls_user(db_session, founder.id)
    db_session.add(SourceRelationship(owner_id=founder.id, from_source_id=report.id, to_source_id=root.id, relationship_type=RelationshipType.derived_from))
    db_session.add(SourceRelationship(owner_id=founder.id, from_source_id=root.id, to_source_id=report.id, relationship_type=RelationshipType.contradicts))
    db_session.commit()

    _ask(client, "Vad säger rotstudien?")
    system = prompt_capture["system"]
    assert "andrangordningseffekter: 1" in system, "the consequence appears when the edge exists"
    assert "contested_ground" in system
    assert str(report.id) in system, "the DEPENDENT source is named — the founder sees WHAT is affected"


def test_workbench_prompt_carries_the_graph_context(db_session, client, prompt_capture):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _doc_with_claim(db_session, founder.id, title="Analyskälla", claim_text="Arbetsytans underlag.")

    _ask(client, "Analysera underlaget", path="/api/workbench/analyze")
    assert "GRAFKONTEXT" in prompt_capture["system"], "workbench invokes the graph layer too"


# --- 3: question-relevant cross-domain overlaps ----------------------------------------------------


def test_cross_domain_overlap_appears_only_when_question_relevant(db_session, client, prompt_capture):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    project_a = Project(name=f"PA {uuid.uuid4().hex[:6]}", created_by=founder.id)
    project_b = Project(name=f"PB {uuid.uuid4().hex[:6]}", created_by=founder.id)
    db_session.add_all([project_a, project_b])
    db_session.commit()
    _doc_with_claim(db_session, founder.id, title="Domän A", claim_text="Kaffe höjer kortisolvärdet.", project_id=project_a.id)
    _doc_with_claim(db_session, founder.id, title="Domän B", claim_text="Kaffe höjer kortisolvärdet.", project_id=project_b.id)

    # Question shares 2+ content words with the overlapped statement -> relevant.
    _ask(client, "Påverkar kaffe kortisolvärdet?")
    assert "korsdomän-överlapp: 1" in prompt_capture["system"]
    assert "Kaffe höjer kortisolvärdet." in prompt_capture["system"]
    assert "2 projekt" in prompt_capture["system"]
    assert "SAMKÖRA, inte bekräftelse" in prompt_capture["system"], "the anti-inflation rule rides the line"

    # An unrelated question -> the SAME graph state produces NO overlap line.
    _ask(client, "Vad gäller för skattesatser denna säsong?")
    assert "korsdomän-överlapp" not in prompt_capture["system"] or "inga frågerelevanta" in prompt_capture["system"]


# --- 4: the topic landscape rides the graph block ---------------------------------------------------


def test_topic_landscape_and_declared_conflict_in_the_prompt(db_session, client, prompt_capture):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _s1, claim_a = _doc_with_claim(db_session, founder.id, title="Studie A", claim_text="Studie A om kaffet.")
    _s2, claim_b = _doc_with_claim(db_session, founder.id, title="Studie B", claim_text="Studie B om kaffet.")
    record_claim_evidence_metadata(
        db_session, founder.id, claim_a.id,
        {"research_topic": "kaffe_puls", "study_design": "randomized_trial", "study_setting": "Sverige", "sample_size": 300},
        declared_via="prompt_test",
    )
    db_session.commit()
    csrf = _login(client)

    _ask(client, "Vad vet vi om kaffe och puls?")
    assert "ÄMNESlandskap 'kaffe_puls'" in prompt_capture["system"], "the topic line rides the graph block"
    assert "avsaknad av konflikt" not in prompt_capture["system"] or "inga deklarerade" in prompt_capture["system"]

    # Declare the conflict inside the topic -> BOTH sides are named in the prompt.
    record_claim_evidence_metadata(
        db_session, founder.id, claim_b.id,
        {"research_topic": "kaffe_puls", "study_design": "prospective_cohort", "study_setting": "Japan", "sample_size": 900},
        declared_via="prompt_test",
    )
    _set_rls_user(db_session, founder.id)
    db_session.add(ClaimRelationship(owner_id=founder.id, from_claim_id=claim_a.id, to_claim_id=claim_b.id, relationship_type=ClaimRelationshipType.contradicts))
    db_session.commit()

    _ask(client, "Vad vet vi om kaffe och puls?")
    system = prompt_capture["system"]
    assert "DEKLARERADE MOTSÄGELSELER: 1" in system
    assert "länder emellan 1" in system, "the cross-country disagreement is visible to the model"
    assert "Studie A om kaffet." in system and "Studie B om kaffet." in system, "both sides named"
    assert "ingen vinner här" in system, "the no-winner rule rides the conflict line"


# --- 5: owner isolation at the prompt layer ---------------------------------------------------------


def test_other_owners_graph_state_never_reaches_the_founders_prompt(db_session, client, prompt_capture, make_verified_user):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    other, _ = make_verified_user()
    # The OTHER owner builds the exact graph state the founder's question would match.
    other_root, other_claim = _doc_with_claim(db_session, other.id, title="Annan owners rot", claim_text="Hemlig markörhöjning hemlig.")
    _set_rls_user(db_session, other.id)
    other_report, _ = _doc_with_claim(db_session, other.id, title="Annan owners rapport", claim_text="Hemlig sammanfattning.")
    db_session.add(SourceRelationship(owner_id=other.id, from_source_id=other_report.id, to_source_id=other_root.id, relationship_type=RelationshipType.derived_from))
    db_session.add(SourceRelationship(owner_id=other.id, from_source_id=other_root.id, to_source_id=other_report.id, relationship_type=RelationshipType.contradicts))
    db_session.commit()

    _set_rls_user(db_session, founder.id)
    _ask(client, "Vet du något om hemlig markörhöjning hemlig?")
    system = prompt_capture["system"]
    assert "Hemlig markörhöjning hemlig." not in system, "another owner's claim text never leaks"
    assert "Annan owners rot" not in system and "Annan owners rapport" not in system
    assert "andrangordningseffekter: 1" not in system, "another owner's graph state is invisible to the founder's prompt"
