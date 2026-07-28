"""STEG 10 (claim-level trust) — app/rag/claims.py's extraction, app/rag/trust.py's
assess_claim_confidence(), and RLS isolation for knowledge_claims/claim_relationships.
Real local Postgres (RLS included), a deterministic fake chat provider (never a real AI
key)."""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.rag.claims import EXTRACTION_VERSION, extract_claims_for_document, grounding_score
from app.rag.trust import assess_claim_confidence
from app.request_context import current_user_id as current_user_id_var

EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_source(session, owner_id, *, status=ActiveTruthStatus.active, project_id=None) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=status, project_id=project_id)
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value="Bolaget grundades 2019 i Stockholm.") -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * EMBEDDING_DIM)
    session.add(chunk)
    session.commit()
    return chunk


def _make_claim(session, owner_id, source_id, *, confidence=ClaimConfidence.likely, claim_text="Bolaget grundades 2019.") -> KnowledgeClaim:
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id,
        source_id=source_id,
        claim_text=claim_text,
        confidence=confidence,
        extraction_version=EXTRACTION_VERSION,
    )
    session.add(claim)
    session.commit()
    return claim


# --- grounding_score: pure function ---


def test_grounding_score_full_overlap_is_one():
    assert grounding_score("Bolaget grundades 2019", "Bolaget grundades ar 2019 i Stockholm.") == 1.0


def test_grounding_score_no_overlap_is_zero():
    assert grounding_score("Katten sitter pa taket idag", "Bolaget grundades 2019 i Stockholm.") == 0.0


def test_grounding_score_empty_claim_is_zero():
    assert grounding_score("", "Bolaget grundades 2019.") == 0.0


# --- extraction: DB integration with a fake chat provider ---


@pytest.fixture
def _fake_claim_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    calls: list[str] = []

    async def _fake_chat(self, messages, model, **kwargs):
        chunk_text = messages[-1].content
        calls.append(chunk_text)
        if "grundades" in chunk_text:
            content = '[{"text": "Bolaget grundades 2019 i Stockholm.", "claim_type": "historical"}]'
        elif "hallucinera" in chunk_text:
            # Deliberately unrelated to the chunk text — simulates a hallucinated claim so
            # the grounding-score-based no_basis path can be tested against a real (if
            # fake) provider response, not just the pure function in isolation.
            content = '[{"text": "Katten har sju liv och bor pa manen.", "claim_type": "uncategorized"}]'
        else:
            content = "[]"
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    return calls


@pytest.mark.asyncio
async def test_extract_claims_binds_source_version_and_chunk(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.source_id == document.id
    assert claim.chunk_id == chunk.id
    assert claim.claim_text == "Bolaget grundades 2019 i Stockholm."
    assert claim.claim_type == ClaimType.historical
    assert claim.extraction_version == EXTRACTION_VERSION


@pytest.mark.asyncio
async def test_well_grounded_claim_gets_likely_not_certain(db_session, make_verified_user, _fake_claim_provider):
    """A single source can never earn "certain" at extraction time — only independent
    corroboration (assess_claim_confidence, tested separately below) can do that."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert claims[0].confidence == ClaimConfidence.likely
    assert claims[0].grounding_score > 0.6


@pytest.mark.asyncio
async def test_hallucinated_claim_is_flagged_no_basis_not_hidden(db_session, make_verified_user, _fake_claim_provider):
    """A claim the fake provider "hallucinated" (text unrelated to its source chunk) is
    still stored — visible for audit — but flagged no_basis, never silently discarded and
    never given a confidence a founder could mistake for something well-supported."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "hallucinera testfall for detta scenario.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1
    assert claims[0].confidence == ClaimConfidence.no_basis
    assert claims[0].grounding_score < 0.3


@pytest.mark.asyncio
async def test_no_claims_extracted_from_a_chunk_with_no_facts(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "inget speciellt har.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert claims == []


@pytest.mark.asyncio
async def test_extraction_is_capped_at_max_chunks_per_document(db_session, make_verified_user, _fake_claim_provider, monkeypatch):
    import app.rag.claims as claims_module

    monkeypatch.setattr(claims_module, "MAX_CHUNKS_PER_DOCUMENT", 2)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    for i in range(5):
        db_session.add(
            DocumentChunk(document_id=document.id, owner_id=user.id, chunk_index=i, text="Bolaget grundades 2019.", embedding=[0.1] * EMBEDDING_DIM)
        )
    db_session.commit()

    await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(_fake_claim_provider) == 2  # one provider call per chunk, capped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_status,expected_claim_status",
    [
        (ActiveTruthStatus.active, ClaimStatus.active),
        (ActiveTruthStatus.historical, ClaimStatus.historical),
        (ActiveTruthStatus.proposed, ClaimStatus.proposed),
        (ActiveTruthStatus.superseded, ClaimStatus.historical),
        (ActiveTruthStatus.disputed, ClaimStatus.disputed),
    ],
)
async def test_claim_status_inherits_from_source_status(db_session, make_verified_user, _fake_claim_provider, source_status, expected_claim_status):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id, status=source_status)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert claims[0].status == expected_claim_status


@pytest.mark.asyncio
async def test_provider_error_on_one_chunk_does_not_abort_the_others(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ChatResult, ProviderError
    from app.providers.openai_provider import OpenAIProvider

    call_count = {"n": 0}

    async def _flaky_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ProviderError("simulerat leverantorsfel")
        return ChatResult(content='["Bolaget grundades 2019."]', provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _flaky_chat)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    for i in range(2):
        db_session.add(
            DocumentChunk(document_id=document.id, owner_id=user.id, chunk_index=i, text="Bolaget grundades 2019.", embedding=[0.1] * EMBEDDING_DIM)
        )
    db_session.commit()

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1  # the first chunk's provider failure was skipped, not fatal


# --- RLS isolation ---


def test_claims_isolated_between_owners(db_session, superuser_db, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a = _make_source(superuser_db, user_a.id)
    _make_claim(superuser_db, user_a.id, doc_a.id)

    _set_rls_user(db_session, user_b.id)
    assert db_session.query(KnowledgeClaim).count() == 0

    _set_rls_user(db_session, user_a.id)
    assert db_session.query(KnowledgeClaim).count() == 1


# --- P3: claim_type extraction ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_type",
    ["idea", "decision", "task_reference", "vision", "technical", "historical", "uncategorized"],
)
async def test_each_valid_claim_type_round_trips(db_session, make_verified_user, monkeypatch, provider_type):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _typed_chat(self, messages, model, **kwargs):
        return ChatResult(
            content=f'[{{"text": "Ett pastaende.", "claim_type": "{provider_type}"}}]',
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _typed_chat)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Ett pastaende i sitt sammanhang.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert claims[0].claim_type == ClaimType(provider_type)


@pytest.mark.asyncio
async def test_unknown_claim_type_from_provider_defaults_to_uncategorized(db_session, make_verified_user, monkeypatch):
    """A provider returning something outside P3's seven allowed values (a typo, a made-up
    category) must never be trusted verbatim and must never be guessed into one of the other
    six real values — it collapses to uncategorized, the claim itself is still kept."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _bogus_type_chat(self, messages, model, **kwargs):
        return ChatResult(
            content='[{"text": "Ett pastaende.", "claim_type": "not_a_real_type"}]',
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _bogus_type_chat)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Ett pastaende i sitt sammanhang.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1
    assert claims[0].claim_type == ClaimType.uncategorized


@pytest.mark.asyncio
async def test_legacy_plain_string_response_still_extracts_with_uncategorized_type(db_session, make_verified_user, monkeypatch):
    """A provider ignoring the updated system prompt and returning the pre-P3 plain-string
    contract (`["text", ...]`) must not have its claims silently dropped — the claim text is
    still extracted, just with claim_type=uncategorized rather than crashing or discarding
    it. Defense in depth against a provider not following instructions, not an expected
    steady-state response shape."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _legacy_chat(self, messages, model, **kwargs):
        return ChatResult(
            content='["Ett pastaende i gammalt format."]', provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3}
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _legacy_chat)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Ett pastaende i gammalt format har hant.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)
    assert len(claims) == 1
    assert claims[0].claim_text == "Ett pastaende i gammalt format."
    assert claims[0].claim_type == ClaimType.uncategorized


def test_claim_type_defaults_to_uncategorized_when_constructed_without_one(db_session, make_verified_user):
    """The model-level default (not just the extraction pipeline's parsing) — a KnowledgeClaim
    row created any other way (e.g. a future migration/backfill path) never ends up with a
    NULL or missing claim_type."""
    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    claim = _make_claim(db_session, user.id, doc.id)
    assert claim.claim_type == ClaimType.uncategorized


# --- assess_claim_confidence ---


class TestAssessClaimConfidence:
    def test_no_relationships_returns_stored_confidence_unchanged(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc = _make_source(db_session, user.id)
        claim = _make_claim(db_session, user.id, doc.id, confidence=ClaimConfidence.uncertain)
        assert assess_claim_confidence(db_session, claim) == ClaimConfidence.uncertain

    def test_no_basis_is_never_promoted_by_support(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc_a = _make_source(db_session, user.id)
        doc_b = _make_source(db_session, user.id)
        weak_claim = _make_claim(db_session, user.id, doc_a.id, confidence=ClaimConfidence.no_basis)
        supporting_claim = _make_claim(db_session, user.id, doc_b.id, confidence=ClaimConfidence.likely)
        _set_rls_user(db_session, user.id)
        db_session.add(
            ClaimRelationship(owner_id=user.id, from_claim_id=supporting_claim.id, to_claim_id=weak_claim.id, relationship_type=ClaimRelationshipType.supports)
        )
        db_session.commit()
        assert assess_claim_confidence(db_session, weak_claim) == ClaimConfidence.no_basis

    def test_independent_support_promotes_likely_to_certain(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc_a = _make_source(db_session, user.id)
        doc_b = _make_source(db_session, user.id)
        target = _make_claim(db_session, user.id, doc_a.id, confidence=ClaimConfidence.likely)
        independent = _make_claim(db_session, user.id, doc_b.id, confidence=ClaimConfidence.likely)
        _set_rls_user(db_session, user.id)
        db_session.add(
            ClaimRelationship(owner_id=user.id, from_claim_id=independent.id, to_claim_id=target.id, relationship_type=ClaimRelationshipType.supports)
        )
        db_session.commit()
        assert assess_claim_confidence(db_session, target) == ClaimConfidence.certain

    def test_support_from_the_same_source_does_not_promote(self, db_session, make_verified_user):
        """Two chunks of the SAME document both stating a fact is not independent
        corroboration — must not be promoted to certain."""
        user, _ = make_verified_user()
        doc = _make_source(db_session, user.id)
        target = _make_claim(db_session, user.id, doc.id, confidence=ClaimConfidence.likely)
        same_source_claim = _make_claim(db_session, user.id, doc.id, confidence=ClaimConfidence.likely, claim_text="Samma kalla, annat stycke.")
        _set_rls_user(db_session, user.id)
        db_session.add(
            ClaimRelationship(owner_id=user.id, from_claim_id=same_source_claim.id, to_claim_id=target.id, relationship_type=ClaimRelationshipType.supports)
        )
        db_session.commit()
        assert assess_claim_confidence(db_session, target) == ClaimConfidence.likely

    def test_contradicts_caps_at_conflict_even_with_support(self, db_session, make_verified_user):
        """A conflict is never silently outvoted by corroboration — same principle as
        app/rag/trust.py's source-level CONFLICT_INSTRUCTION, which always surfaces a
        detected conflict regardless of how strong the matching source's score is."""
        user, _ = make_verified_user()
        doc_a = _make_source(db_session, user.id)
        doc_b = _make_source(db_session, user.id)
        doc_c = _make_source(db_session, user.id)
        target = _make_claim(db_session, user.id, doc_a.id, confidence=ClaimConfidence.likely)
        supporter = _make_claim(db_session, user.id, doc_b.id, confidence=ClaimConfidence.likely)
        contradictor = _make_claim(db_session, user.id, doc_c.id, confidence=ClaimConfidence.likely, claim_text="Motsatt pastaende.")
        _set_rls_user(db_session, user.id)
        db_session.add_all(
            [
                ClaimRelationship(owner_id=user.id, from_claim_id=supporter.id, to_claim_id=target.id, relationship_type=ClaimRelationshipType.supports),
                ClaimRelationship(owner_id=user.id, from_claim_id=contradictor.id, to_claim_id=target.id, relationship_type=ClaimRelationshipType.contradicts),
            ]
        )
        db_session.commit()
        assert assess_claim_confidence(db_session, target) == ClaimConfidence.conflict

    def test_a_session_scoped_to_another_owner_cannot_see_this_claims_relationships(self, db_session, superuser_db, make_verified_user):
        """Defense in depth: even if a claim object from owner A were somehow handed to
        code running with a session RLS-scoped to owner B (a bug elsewhere, not something
        that should normally happen), RLS itself — not just the explicit owner_id filter in
        the query — must prevent B's session from reading A's supports relationship. Proven
        by creating the real supports edge as owner A (which WOULD promote likely->certain,
        per the earlier passing test) and confirming a B-scoped session still sees `likely`,
        not `certain`."""
        user_a, _ = make_verified_user()
        user_b, _ = make_verified_user()
        doc_a = _make_source(superuser_db, user_a.id)
        doc_a2 = _make_source(superuser_db, user_a.id)
        target = _make_claim(superuser_db, user_a.id, doc_a.id, confidence=ClaimConfidence.likely)
        independent = _make_claim(superuser_db, user_a.id, doc_a2.id, confidence=ClaimConfidence.likely)
        _set_rls_user(superuser_db, user_a.id)
        superuser_db.add(
            ClaimRelationship(owner_id=user_a.id, from_claim_id=independent.id, to_claim_id=target.id, relationship_type=ClaimRelationshipType.supports)
        )
        superuser_db.commit()

        _set_rls_user(db_session, user_b.id)
        assert assess_claim_confidence(db_session, target) == ClaimConfidence.likely  # NOT certain — B's session can't see A's supports edge
