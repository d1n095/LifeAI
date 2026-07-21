"""STEG 10 (claim-level trust): extracts discrete, testable factual claims from a document's
already-indexed chunks and binds each one to the exact source/version/chunk it came from —
see app/models/knowledge_claim.py and docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's DEL 8 section.

Reuses the existing provider abstraction (app.providers.registry.chat_with_fallback) the
same way app/routers/chat.py and app/routers/workbench.py already do — no separate
extraction-specific provider wiring, no real AI key in tests (see
tests/backend/test_claims.py's deterministic fake chat provider).
"""

import json
import re
import uuid

from sqlalchemy.orm import Session

from app.models.document import ActiveTruthStatus, Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, KnowledgeClaim
from app.providers.base import Message, ProviderError
from app.providers.registry import chat_with_fallback

EXTRACTION_VERSION = "claims-v1"
MAX_CLAIMS_PER_CHUNK = 8
# Cost bound (DEL 14): one provider call per chunk, capped per document rather than
# unbounded — a document with more chunks than this simply gets claims for its first N
# chunks (a documented trade-off), not an aborted import and not an unbounded-cost call.
MAX_CHUNKS_PER_DOCUMENT = 20

CLAIM_EXTRACTION_SYSTEM_PROMPT = (
    "Du extraherar diskreta, testbara sakpåståenden ur en text. Svara ENDAST med en JSON-lista "
    "av strängar, ett påstående per sträng — inget annat, ingen förklaring runt omkring. Ta "
    "bara med påståenden som uttryckligen stöds av texten. Hitta inte på något som inte står "
    "där. Om texten inte innehåller några tydliga sakpåståenden, svara med en tom lista []."
)

# Document.ActiveTruthStatus has one more value (`superseded`) than ClaimStatus does — a
# superseded SOURCE's claims become historical (no-longer-current), not "proposed" by
# default, since "proposed" specifically means not-yet-decided, which superseded material
# already was decided-and-then-replaced.
_SOURCE_STATUS_TO_CLAIM_STATUS: dict[ActiveTruthStatus, ClaimStatus] = {
    ActiveTruthStatus.active: ClaimStatus.active,
    ActiveTruthStatus.historical: ClaimStatus.historical,
    ActiveTruthStatus.proposed: ClaimStatus.proposed,
    ActiveTruthStatus.superseded: ClaimStatus.historical,
    ActiveTruthStatus.disputed: ClaimStatus.disputed,
}

_WORD_RE = re.compile(r"[a-zA-ZåäöÅÄÖ0-9]+")


def _words(value: str) -> set[str]:
    return {w for w in _WORD_RE.findall(value.lower()) if len(w) > 2}


def grounding_score(claim_text: str, chunk_text: str) -> float:
    """Cheap, auditable word-overlap ratio between an extracted claim and the chunk it was
    extracted from — same heuristic style as app/context/resolver.py's _topic_overlap, not a
    claim of semantic understanding. This objective score (not the extracting model's own
    self-reported confidence) is what claim confidence is actually derived from — see
    _confidence_for_score below and app/rag/trust.py's assess_claim_confidence(), matching
    the same "never trust the model's self-assessment" principle already applied at the
    source level in this file's sibling."""
    claim_words = _words(claim_text)
    if not claim_words:
        return 0.0
    chunk_words = _words(chunk_text)
    if not chunk_words:
        return 0.0
    return len(claim_words & chunk_words) / len(claim_words)


def _confidence_for_score(score: float) -> ClaimConfidence:
    # A single, uncorroborated source can never earn "certain" here regardless of how well
    # the claim text overlaps its own source chunk — "certain" is only ever reached via
    # independent corroboration, computed dynamically in app/rag/trust.py's
    # assess_claim_confidence(), never at extraction time.
    if score >= 0.6:
        return ClaimConfidence.likely
    if score >= 0.3:
        return ClaimConfidence.uncertain
    return ClaimConfidence.no_basis


def _parse_claims(raw: str) -> list[str]:
    """Providers occasionally wrap JSON in prose or a code fence despite the system prompt's
    instruction — pulls out the first well-formed JSON array found rather than requiring an
    exact match. Returns [] (never raises) on anything unparseable: a malformed extraction
    response must not crash the import it's attached to."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


async def extract_claims_for_document(
    db: Session,
    document: Document,
    owner_id: uuid.UUID,
    version_id: uuid.UUID | None,
) -> list[KnowledgeClaim]:
    """Runs claim extraction over a document's already-indexed chunks (called right after
    app/rag/ingest.py's index_document succeeds — see app/rag/library_import.py). The
    caller's session must already have app.current_user_id set to owner_id (same convention
    index_document itself requires, see its docstring) — this function does not set it
    itself, since it's always called from a context that already has.

    A single chunk's extraction failure (a provider error, an unparseable response) skips
    just that chunk, never aborts the document's import — matching this codebase's
    established "one file's/one chunk's error must not corrupt the whole batch" principle
    (see app/rag/library_import.py's per-file FileOutcome handling).
    """
    chunks = (
        db.query(DocumentChunk)
        .filter_by(document_id=document.id, owner_id=owner_id)
        .order_by(DocumentChunk.chunk_index)
        .limit(MAX_CHUNKS_PER_DOCUMENT)
        .all()
    )
    claim_status = _SOURCE_STATUS_TO_CLAIM_STATUS.get(document.active_truth_status, ClaimStatus.proposed)

    created: list[KnowledgeClaim] = []
    for chunk in chunks:
        messages = [
            Message(role="system", content=CLAIM_EXTRACTION_SYSTEM_PROMPT),
            Message(role="user", content=chunk.text[:4000]),
        ]
        try:
            result, _ = await chat_with_fallback(db, messages)
        except ProviderError:
            continue

        for claim_text in _parse_claims(result.content)[:MAX_CLAIMS_PER_CHUNK]:
            score = grounding_score(claim_text, chunk.text)
            claim = KnowledgeClaim(
                owner_id=owner_id,
                source_id=document.id,
                version_id=version_id,
                chunk_id=chunk.id,
                project_id=document.project_id,
                claim_text=claim_text,
                status=claim_status,
                confidence=_confidence_for_score(score),
                grounding_score=score,
                extraction_version=EXTRACTION_VERSION,
            )
            db.add(claim)
            created.append(claim)

    if created:
        db.commit()
    return created
