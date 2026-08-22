"""Proves app/rag/claims.py's extract_claims_for_document() -> interpretation_proposals live
wiring: a decision/idea/task_reference-typed claim gets a candidate interpretation_proposal
recorded (never a project_entities row directly); a historical/technical/vision/uncategorized
claim does not. Mirrors tests/backend/chat/test_chat_candidate_signal_capture.py's own
established pattern for proving a live signal-producer -> staging-layer wiring."""

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.project_entities import InterpretationProposal
from app.rag.claims import extract_claims_for_document
from app.request_context import current_user_id as current_user_id_var

EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_source(session, owner_id) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value) -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * EMBEDDING_DIM)
    session.add(chunk)
    session.commit()
    return chunk


@pytest.fixture
def _fake_claim_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_chat(self, messages, model, **kwargs):
        chunk_text = messages[-1].content
        if "byta databas" in chunk_text:
            content = '[{"text": "Vi bör byta databas till Postgres.", "claim_type": "decision"}]'
        elif "grundades" in chunk_text:
            content = '[{"text": "Bolaget grundades 2019.", "claim_type": "historical"}]'
        else:
            content = "[]"
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


@pytest.mark.asyncio
async def test_a_decision_claim_gets_a_candidate_interpretation_proposal_never_a_project_entity(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Vi ska besluta om att byta databas.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1
    claim = claims[0]

    proposals = db_session.query(InterpretationProposal).filter_by(source_claim_id=claim.id).all()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.proposed_entity_type == "decision"
    assert proposal.status == "unreviewed"
    assert proposal.promoted_to_entity_id is None
    assert proposal.classifier_strategy == "claim_type_extraction_v1"


@pytest.mark.asyncio
async def test_a_historical_claim_gets_no_interpretation_proposal(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1
    claim = claims[0]

    proposals = db_session.query(InterpretationProposal).filter_by(source_claim_id=claim.id).all()
    assert proposals == []


@pytest.mark.asyncio
async def test_a_failure_recording_the_interpretation_proposal_never_breaks_claim_extraction(db_session, make_verified_user, _fake_claim_provider, monkeypatch):
    """The non-fatal, swallowed-error guarantee -- same doctrine as
    app/routers/chat.py's own candidate-signal wiring: a bug in the observational side-effect
    can never take down the caller's own main result."""

    import app.rag.claims as claims_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated interpretation_proposals failure")

    monkeypatch.setattr(claims_module, "record_interpretation_proposal", _boom)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Vi ska besluta om att byta databas.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1  # claim extraction itself still succeeded
