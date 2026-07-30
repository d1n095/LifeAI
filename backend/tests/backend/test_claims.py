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
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import DocumentSourceUnit, MemorySourceUnit, SnapshotStatus, SourceKind
from app.rag.claims import BACKFILL_BATCH_SIZE, EXTRACTION_VERSION, backfill_claim_types, extract_claims_for_document, grounding_score
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


def _make_claim(
    session,
    owner_id,
    source_id,
    *,
    confidence=ClaimConfidence.likely,
    claim_text="Bolaget grundades 2019.",
    claim_type=ClaimType.uncategorized,
    extraction_version=EXTRACTION_VERSION,
) -> KnowledgeClaim:
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id,
        source_id=source_id,
        claim_text=claim_text,
        confidence=confidence,
        claim_type=claim_type,
        extraction_version=extraction_version,
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


# --- P3 backfill: retroactive claim_type for pre-existing rows -----------------------------


@pytest.mark.asyncio
async def test_backfill_types_legacy_uncategorized_claims(db_session, make_verified_user, monkeypatch):
    import json as json_module

    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_backfill_chat(self, messages, model, **kwargs):
        texts = json_module.loads(messages[-1].content)
        return ChatResult(
            content=json_module.dumps(["decision" for _ in texts]), provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3}
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_backfill_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    claim_a = _make_claim(db_session, user.id, doc.id, extraction_version="claims-v1")
    claim_b = _make_claim(db_session, user.id, doc.id, claim_text="Ett annat gammalt pastaende.", extraction_version="claims-v1")

    before_count = db_session.query(KnowledgeClaim).count()
    result = await backfill_claim_types(db_session, user.id)

    assert result.candidates_total == 2
    assert result.typed == 2
    assert result.still_uncategorized == 0
    assert result.failed == 0
    assert db_session.query(KnowledgeClaim).count() == before_count  # no new rows

    db_session.expire_all()
    assert db_session.get(KnowledgeClaim, claim_a.id).claim_type == ClaimType.decision
    assert db_session.get(KnowledgeClaim, claim_b.id).claim_type == ClaimType.decision


@pytest.mark.asyncio
async def test_backfill_preserves_existing_ids_and_provenance(db_session, make_verified_user, monkeypatch):
    import json as json_module

    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_backfill_chat(self, messages, model, **kwargs):
        texts = json_module.loads(messages[-1].content)
        return ChatResult(content=json_module.dumps(["idea" for _ in texts]), provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_backfill_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, doc.id)
    _set_rls_user(db_session, user.id)
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=doc.id,
        chunk_id=chunk.id,
        claim_text="Ett spårbart pastaende.",
        status=ClaimStatus.active,
        confidence=ClaimConfidence.likely,
        grounding_score=0.42,
        extraction_version="claims-v1",
    )
    db_session.add(claim)
    db_session.commit()
    claim_id, source_id, chunk_id = claim.id, claim.source_id, claim.chunk_id

    await backfill_claim_types(db_session, user.id)

    db_session.expire_all()
    refreshed = db_session.get(KnowledgeClaim, claim_id)
    assert refreshed.id == claim_id
    assert refreshed.source_id == source_id
    assert refreshed.chunk_id == chunk_id
    assert refreshed.status == ClaimStatus.active
    assert refreshed.confidence == ClaimConfidence.likely
    assert refreshed.grounding_score == 0.42
    assert refreshed.claim_type == ClaimType.idea  # only claim_type (and extraction_version) changed


@pytest.mark.asyncio
async def test_backfill_second_run_is_a_no_op(db_session, make_verified_user, monkeypatch):
    import json as json_module

    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    call_count = {"n": 0}

    async def _fake_backfill_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        texts = json_module.loads(messages[-1].content)
        return ChatResult(content=json_module.dumps(["technical" for _ in texts]), provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_backfill_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    _make_claim(db_session, user.id, doc.id, extraction_version="claims-v1")

    first = await backfill_claim_types(db_session, user.id)
    assert first.candidates_total == 1
    assert call_count["n"] == 1

    second = await backfill_claim_types(db_session, user.id)
    assert second.candidates_total == 0  # nothing left to classify
    assert call_count["n"] == 1  # no second provider call made


@pytest.mark.asyncio
async def test_backfill_never_overwrites_an_already_typed_claim(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("an already-typed claim must never trigger a backfill provider call")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    # Already typed by the real extraction pipeline (current EXTRACTION_VERSION) — not a
    # backfill candidate even though claim_type happens to be a real, non-uncategorized value.
    claim = _make_claim(db_session, user.id, doc.id, claim_type=ClaimType.vision, extraction_version=EXTRACTION_VERSION)

    result = await backfill_claim_types(db_session, user.id)

    assert result.candidates_total == 0
    db_session.expire_all()
    assert db_session.get(KnowledgeClaim, claim.id).claim_type == ClaimType.vision


@pytest.mark.asyncio
async def test_backfill_provider_error_leaves_claim_uncategorized_and_retryable(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.openai_provider import OpenAIProvider

    async def _always_fails(self, messages, model, **kwargs):
        raise ProviderError("simulerat leverantorsfel")

    monkeypatch.setattr(OpenAIProvider, "chat", _always_fails)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    claim = _make_claim(db_session, user.id, doc.id, extraction_version="claims-v1")

    result = await backfill_claim_types(db_session, user.id)

    assert result.candidates_total == 1
    assert result.failed == 1
    assert result.typed == 0
    db_session.expire_all()
    refreshed = db_session.get(KnowledgeClaim, claim.id)
    assert refreshed.claim_type == ClaimType.uncategorized
    assert refreshed.extraction_version == "claims-v1"  # untouched — still a candidate next run


@pytest.mark.asyncio
async def test_backfill_length_mismatch_response_leaves_claims_retryable(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _mismatched_length_chat(self, messages, model, **kwargs):
        # Two claims are being classified but the provider only answers for one — must never
        # be trusted to line up positionally.
        return ChatResult(content='["decision"]', provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _mismatched_length_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    claim_a = _make_claim(db_session, user.id, doc.id, extraction_version="claims-v1")
    claim_b = _make_claim(db_session, user.id, doc.id, claim_text="Nagot annat.", extraction_version="claims-v1")

    result = await backfill_claim_types(db_session, user.id)

    assert result.failed == 2
    assert result.typed == 0
    db_session.expire_all()
    assert db_session.get(KnowledgeClaim, claim_a.id).claim_type == ClaimType.uncategorized
    assert db_session.get(KnowledgeClaim, claim_b.id).claim_type == ClaimType.uncategorized


@pytest.mark.asyncio
async def test_backfill_genuinely_uncategorized_result_settles_and_is_not_retried(db_session, make_verified_user, monkeypatch):
    """A model honestly answering "uncategorized" for an ambiguous claim must not be
    re-queried forever — it settles (extraction_version bumped) exactly like a real type
    would, distinguishing it from a provider FAILURE (which stays retryable, see the sibling
    test above)."""
    import json as json_module

    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    call_count = {"n": 0}

    async def _fake_backfill_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        texts = json_module.loads(messages[-1].content)
        return ChatResult(
            content=json_module.dumps(["uncategorized" for _ in texts]), provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3}
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_backfill_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    claim = _make_claim(db_session, user.id, doc.id, extraction_version="claims-v1")

    first = await backfill_claim_types(db_session, user.id)
    assert first.still_uncategorized == 1
    assert first.typed == 0

    db_session.expire_all()
    refreshed = db_session.get(KnowledgeClaim, claim.id)
    assert refreshed.claim_type == ClaimType.uncategorized
    assert refreshed.extraction_version == EXTRACTION_VERSION  # settled, not a candidate anymore

    second = await backfill_claim_types(db_session, user.id)
    assert second.candidates_total == 0
    assert call_count["n"] == 1  # no second provider call


@pytest.mark.asyncio
async def test_backfill_respects_max_batches_per_call(db_session, make_verified_user, monkeypatch):
    """2026-07-28 correction: the admin endpoint bounds max_batches so a large library can't
    hold one HTTP request open indefinitely — this is the underlying mechanism that bound
    relies on. More candidates than one batch covers, capped to max_batches=1, must leave the
    rest untouched (still valid candidates) for a follow-up call."""
    import json as json_module

    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_backfill_chat(self, messages, model, **kwargs):
        texts = json_module.loads(messages[-1].content)
        return ChatResult(content=json_module.dumps(["technical" for _ in texts]), provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_backfill_chat)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id)
    total_claims = BACKFILL_BATCH_SIZE + 5
    for i in range(total_claims):
        _make_claim(db_session, user.id, doc.id, claim_text=f"Gammalt pastaende nummer {i}.", extraction_version="claims-v1")

    result = await backfill_claim_types(db_session, user.id, max_batches=1)

    assert result.candidates_total == BACKFILL_BATCH_SIZE  # only the first batch was processed
    remaining = (
        db_session.query(KnowledgeClaim)
        .filter(KnowledgeClaim.claim_type == ClaimType.uncategorized, KnowledgeClaim.extraction_version == "claims-v1")
        .count()
    )
    assert remaining == 5  # the rest are still valid candidates for a follow-up call


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


# --- S1A dual-write (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, phase 3) ---------------


@pytest.mark.asyncio
async def test_dual_write_single_claim_gets_memory_source_id(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    chunk = _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.memory_source_id is not None
    msu = db_session.get(MemorySourceUnit, claim.memory_source_id)
    assert msu.source_kind == SourceKind.document_chunk
    assert msu.snapshot_status == SnapshotStatus.exact
    assert msu.content_text == chunk.text
    dsu = db_session.get(DocumentSourceUnit, claim.memory_source_id)
    assert dsu.document_id == document.id
    assert dsu.chunk_id == chunk.id


@pytest.mark.asyncio
async def test_dual_write_multiple_claims_from_same_chunk_share_memory_source_id(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _two_claims_chat(self, messages, model, **kwargs):
        return ChatResult(
            content=(
                '[{"text": "Forsta pastaendet.", "claim_type": "historical"}, '
                '{"text": "Andra pastaendet.", "claim_type": "technical"}]'
            ),
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _two_claims_chat)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Nagot med tva pastaenden.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert len(claims) == 2
    assert claims[0].memory_source_id is not None
    assert claims[0].memory_source_id == claims[1].memory_source_id
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 1


@pytest.mark.asyncio
async def test_dual_write_multiple_chunks_get_distinct_memory_source_ids(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    db_session.add(
        DocumentChunk(document_id=document.id, owner_id=user.id, chunk_index=0, text="Bolaget grundades 2019 i Stockholm.", embedding=[0.1] * EMBEDDING_DIM)
    )
    db_session.add(
        DocumentChunk(document_id=document.id, owner_id=user.id, chunk_index=1, text="hallucinera testfall for detta scenario.", embedding=[0.1] * EMBEDDING_DIM)
    )
    db_session.commit()

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert len(claims) == 2
    memory_source_ids = {c.memory_source_id for c in claims}
    assert None not in memory_source_ids
    assert len(memory_source_ids) == 2  # one distinct source unit per chunk
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 2


@pytest.mark.asyncio
async def test_dual_write_provider_error_creates_no_orphan_source_unit(db_session, make_verified_user, monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.openai_provider import OpenAIProvider

    async def _always_fails(self, messages, model, **kwargs):
        raise ProviderError("simulerat leverantorsfel")

    monkeypatch.setattr(OpenAIProvider, "chat", _always_fails)

    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert claims == []
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_empty_extraction_creates_no_source_unit(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "inget speciellt har.")  # the fake provider returns []

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=None)

    assert claims == []
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_transaction_rollback_leaves_no_orphan_source_or_claim(db_session, make_verified_user, _fake_claim_provider, monkeypatch):
    """A crash between the (uncommitted, SAVEPOINT-flushed) source-unit insert and the final
    db.commit() must roll back the whole transaction — never a committed MemorySourceUnit
    with no claim actually pointing to it, and never a committed claim missing its
    memory_source_id."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    real_commit = db_session.commit

    def _boom():
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(db_session, "commit", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await extract_claims_for_document(db_session, document, user.id, version_id=None)

    monkeypatch.setattr(db_session, "commit", real_commit)
    db_session.rollback()

    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user.id).count() == 0
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_correct_owner_source_version_chunk_linkage(db_session, make_verified_user, _fake_claim_provider):
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    version = KnowledgeVersion(source_id=document.id, owner_id=user.id, version_number=1, checksum="c" * 64, extraction_version="v1")
    db_session.add(version)
    db_session.commit()
    chunk = _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=version.id)

    assert len(claims) == 1
    claim = claims[0]
    msu = db_session.get(MemorySourceUnit, claim.memory_source_id)
    assert msu.owner_id == user.id
    dsu = db_session.get(DocumentSourceUnit, claim.memory_source_id)
    assert dsu.owner_id == user.id
    assert dsu.document_id == document.id
    assert dsu.chunk_id == chunk.id
    assert dsu.version_id is None  # dual-write always creates document_chunk sources, never document_version


@pytest.mark.asyncio
async def test_dual_write_preserves_legacy_provenance_columns(db_session, make_verified_user, _fake_claim_provider):
    """The old source_id/version_id/chunk_id columns must stay exactly as before —
    memory_source_id is additive, not a replacement, during the cutover (§4.8's six-phase
    plan)."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    version = KnowledgeVersion(source_id=document.id, owner_id=user.id, version_number=1, checksum="d" * 64, extraction_version="v1")
    db_session.add(version)
    db_session.commit()
    chunk = _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=version.id)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.source_id == document.id
    assert claim.version_id == version.id
    assert claim.chunk_id == chunk.id
    assert claim.memory_source_id is not None


# --- S1A dual-write: ownership/version integrity fails closed (Pass 19 review) --------------


@pytest.mark.asyncio
async def test_dual_write_rejects_version_belonging_to_a_different_document(db_session, make_verified_user, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider
    from app.rag.claims import ClaimExtractionIntegrityError

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("no provider call may happen when ownership/version verification fails closed")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    user, _ = make_verified_user()
    document_a = _make_source(db_session, user.id, project_id=None)
    document_b = _make_source(db_session, user.id, project_id=None)
    _make_chunk(db_session, user.id, document_a.id, "Bolaget grundades 2019 i Stockholm.")
    _set_rls_user(db_session, user.id)
    version_of_b = KnowledgeVersion(source_id=document_b.id, owner_id=user.id, version_number=1, checksum="e" * 64, extraction_version="v1")
    db_session.add(version_of_b)
    db_session.commit()

    with pytest.raises(ClaimExtractionIntegrityError, match="does not structurally belong"):
        await extract_claims_for_document(db_session, document_a, user.id, version_id=version_of_b.id)

    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user.id).count() == 0
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_rejects_version_belonging_to_a_different_owner(db_session, make_verified_user, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider
    from app.rag.claims import ClaimExtractionIntegrityError

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("no provider call may happen when ownership/version verification fails closed")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    document_a = _make_source(db_session, user_a.id)
    _make_chunk(db_session, user_a.id, document_a.id, "Bolaget grundades 2019 i Stockholm.")
    _set_rls_user(db_session, user_b.id)
    document_b_for_version = _make_source(db_session, user_b.id)
    version_of_b = KnowledgeVersion(
        source_id=document_b_for_version.id, owner_id=user_b.id, version_number=1, checksum="f" * 64, extraction_version="v1"
    )
    db_session.add(version_of_b)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    with pytest.raises(ClaimExtractionIntegrityError, match="does not structurally belong"):
        await extract_claims_for_document(db_session, document_a, user_a.id, version_id=version_of_b.id)

    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user_a.id).count() == 0
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user_a.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_rejects_document_not_owned_by_the_given_owner_id(db_session, make_verified_user, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider
    from app.rag.claims import ClaimExtractionIntegrityError

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("no provider call may happen when ownership verification fails closed")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    document_a = _make_source(db_session, user_a.id)
    _make_chunk(db_session, user_a.id, document_a.id, "Bolaget grundades 2019 i Stockholm.")

    with pytest.raises(ClaimExtractionIntegrityError, match="is not owned by"):
        await extract_claims_for_document(db_session, document_a, user_b.id, version_id=None)

    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user_b.id).count() == 0
    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user_b.id).count() == 0


@pytest.mark.asyncio
async def test_dual_write_accepts_a_genuinely_matching_version(db_session, make_verified_user, _fake_claim_provider):
    """Positive counterpart: a version that genuinely belongs to the same document/owner is
    accepted and extraction proceeds normally — the integrity check isn't overly strict."""
    user, _ = make_verified_user()
    document = _make_source(db_session, user.id)
    _set_rls_user(db_session, user.id)
    version = KnowledgeVersion(source_id=document.id, owner_id=user.id, version_number=1, checksum="1" * 64, extraction_version="v1")
    db_session.add(version)
    db_session.commit()
    _make_chunk(db_session, user.id, document.id, "Bolaget grundades 2019 i Stockholm.")

    claims = await extract_claims_for_document(db_session, document, user.id, version_id=version.id)
    assert len(claims) == 1
    assert claims[0].version_id == version.id
