"""Regression coverage for the Life Vault / External-AI Egress Control gate on the claim
pipeline (app/rag/claims.py). Before this fix, extract_claims_for_document() and
backfill_claim_types() both called providers.registry.chat_with_fallback() WITHOUT owner_id,
so enforce_egress_policy() was skipped entirely — the owner's real document/claim text reached
external providers unaudited, and NEVER_EGRESS-marked content was never blocked.

These tests use a deterministic provider spy (never a real AI key) and a real local Postgres
(the disclosure ledger and RLS are DB-level). Each test asserts a property that FAILS against
the pre-fix code: a disclosure-ledger row is written per attempt (proving owner_id now flows
to the gate), and NEVER_EGRESS content is refused before any provider sees it (fail closed).
"""

import json

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.rag.claims import backfill_claim_types, extract_claims_for_document
from app.request_context import current_user_id as current_user_id_var

EMBEDDING_DIM = get_settings().embedding_dim

# One of app/egress_policy/service.py's hard-deny markers. Content carrying it must never be
# sent to any provider — the whole call is refused.
NEVER_EGRESS_TEXT = "NEVER_EGRESS: bankkod 1234 — får aldrig lämna valvet."


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_source(session, owner_id) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title="Källa",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value) -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(
        document_id=document_id,
        owner_id=owner_id,
        chunk_index=0,
        text=text_value,
        embedding=[0.1] * EMBEDDING_DIM,
    )
    session.add(chunk)
    session.commit()
    return chunk


def _make_uncategorized_claim(session, owner_id, source_id, claim_text) -> KnowledgeClaim:
    """A backfill candidate: uncategorized AND an extraction_version that is NOT the current
    one (so it matches backfill_claim_types' candidate filter)."""
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id,
        source_id=source_id,
        claim_text=claim_text,
        confidence=ClaimConfidence.likely,
        claim_type=ClaimType.uncategorized,
        status=ClaimStatus.proposed,
        extraction_version="claims-v0-legacy",
    )
    session.add(claim)
    session.commit()
    return claim


def _disclosure_events(session, owner_id, *, purpose=None):
    _set_rls_user(session, owner_id)
    query = session.query(ProviderDisclosureEvent).filter(ProviderDisclosureEvent.owner_id == owner_id)
    if purpose is not None:
        query = query.filter(ProviderDisclosureEvent.purpose == purpose)
    return query.all()


@pytest.fixture
def _spy_provider(monkeypatch):
    """Records exactly what content each provider call received, and returns a deterministic
    claim-extraction / backfill response. If the egress gate is doing its job, a denied call
    never reaches this spy."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    seen_payloads: list[str] = []

    async def _fake_chat(self, messages, model, **kwargs):
        user_content = messages[-1].content
        seen_payloads.append(user_content)
        # Shape the response for whichever caller is exercising us.
        if user_content.strip().startswith("["):
            # backfill: a JSON list of claim texts -> return a 1:1 list of types.
            try:
                count = len(json.loads(user_content))
            except Exception:
                count = 1
            return ChatResult(
                content=json.dumps(["historical"] * count),
                provider="openai",
                model=model,
                raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return ChatResult(
            content='[{"text": "Bolaget grundades 2019.", "claim_type": "historical"}]',
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    return seen_payloads


@pytest.mark.asyncio
async def test_extract_records_owner_scoped_disclosure_event(db_session, make_verified_user, _spy_provider):
    """A normal extraction now routes through the egress gate: one allowed disclosure-ledger
    row, owner-scoped, attributed to this call. (Zero rows against the pre-fix code.)"""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1  # legitimate extraction still works through the gate

    events = _disclosure_events(db_session, user.id, purpose="claim_extraction")
    assert len(events) >= 1
    assert all(e.decision == "allowed" for e in events)
    assert any("extract_claims_for_document" in e.requested_by for e in events)


@pytest.mark.asyncio
async def test_extract_fails_closed_on_never_egress_chunk(db_session, make_verified_user, _spy_provider):
    """A chunk carrying a NEVER_EGRESS marker must never reach a provider, must yield no
    claim, and must leave a durable denied disclosure-ledger row."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, NEVER_EGRESS_TEXT)

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert claims == []  # fail closed: nothing extracted from protected content
    assert all(NEVER_EGRESS_TEXT not in payload for payload in _spy_provider)  # provider never saw it
    denied = [e for e in _disclosure_events(db_session, user.id, purpose="claim_extraction") if e.decision == "denied"]
    assert len(denied) >= 1
    assert any("never_egress_marker" in (e.redaction_categories or []) for e in denied)


@pytest.mark.asyncio
async def test_backfill_records_disclosure_and_reclassifies(db_session, make_verified_user, _spy_provider):
    """backfill_claim_types now gates its outbound claim text: an allowed disclosure row is
    written and the candidate is reclassified."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    claim = _make_uncategorized_claim(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    result = await backfill_claim_types(db_session, user.id, max_batches=1)
    assert result.failed == 0

    _set_rls_user(db_session, user.id)
    db_session.refresh(claim)
    assert claim.claim_type == ClaimType.historical  # reclassified via the gated call

    events = _disclosure_events(db_session, user.id, purpose="claim_type_backfill")
    assert len(events) >= 1
    assert all(e.decision == "allowed" for e in events)
    assert any("backfill_claim_types" in e.requested_by for e in events)


@pytest.mark.asyncio
async def test_backfill_fails_closed_on_never_egress_claim(db_session, make_verified_user, _spy_provider):
    """A candidate whose claim text carries a NEVER_EGRESS marker must not be disclosed: the
    batch is refused (counted as failed, not reclassified), the provider never sees it, and a
    denied disclosure row is recorded."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    claim = _make_uncategorized_claim(db_session, user.id, document.id, NEVER_EGRESS_TEXT)

    result = await backfill_claim_types(db_session, user.id, max_batches=1)

    assert result.failed >= 1  # fail closed: batch refused, not silently sent
    _set_rls_user(db_session, user.id)
    db_session.refresh(claim)
    assert claim.claim_type == ClaimType.uncategorized  # never reclassified
    assert all(NEVER_EGRESS_TEXT not in payload for payload in _spy_provider)
    denied = [e for e in _disclosure_events(db_session, user.id, purpose="claim_type_backfill") if e.decision == "denied"]
    assert len(denied) >= 1
