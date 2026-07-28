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
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.document import ActiveTruthStatus, Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.providers.base import Message, ProviderError
from app.providers.registry import chat_with_fallback

EXTRACTION_VERSION = "claims-v2"
MAX_CLAIMS_PER_CHUNK = 8
# Cost bound (DEL 14): one provider call per chunk, capped per document rather than
# unbounded — a document with more chunks than this simply gets claims for its first N
# chunks (a documented trade-off), not an aborted import and not an unbounded-cost call.
MAX_CHUNKS_PER_DOCUMENT = 20

# P3 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.1): the exact seven values the plan
# specifies — anything else a provider returns collapses to "uncategorized" (see
# _coerce_claim_type below), never silently dropped and never guessed into one of the other
# six just because the model said so.
_VALID_CLAIM_TYPES = {t.value for t in ClaimType}

CLAIM_EXTRACTION_SYSTEM_PROMPT = (
    "Du extraherar diskreta, testbara sakpåståenden ur en text. Svara ENDAST med en JSON-lista "
    "av objekt, ett per påstående, på formen "
    '{"text": "<påståendet>", "claim_type": "<typ>"} — inget annat, ingen förklaring runt '
    "omkring. <typ> måste vara EXAKT ett av: idea, decision, task_reference, vision, "
    "technical, historical, uncategorized — använd uncategorized om du är osäker, gissa "
    "aldrig. Ta bara med påståenden som uttryckligen stöds av texten. Hitta inte på något som "
    "inte står där. Om texten inte innehåller några tydliga sakpåståenden, svara med en tom "
    "lista []."
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


def _coerce_claim_type(value: object) -> ClaimType:
    """Never trusts the provider's own string verbatim — anything not exactly one of P3's
    seven values (a typo, a made-up category, a wrong type entirely) collapses to
    `uncategorized` rather than being guessed into one of the other six or raising."""
    if isinstance(value, str) and value in _VALID_CLAIM_TYPES:
        return ClaimType(value)
    return ClaimType.uncategorized


def _parse_claims(raw: str) -> list[tuple[str, ClaimType]]:
    """Providers occasionally wrap JSON in prose or a code fence despite the system prompt's
    instruction — pulls out the first well-formed JSON array found rather than requiring an
    exact match. Returns [] (never raises) on anything unparseable: a malformed extraction
    response must not crash the import it's attached to.

    P3: the system prompt now asks for `{"text": ..., "claim_type": ...}` objects, but a
    provider ignoring instructions and returning plain strings (the pre-P3 contract) is
    handled the same defensive way — treated as claim_type=uncategorized rather than
    discarded, so a claim already worth extracting is never silently dropped just because
    its type couldn't be determined."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []

    results: list[tuple[str, ClaimType]] = []
    for item in parsed:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            claim_type = _coerce_claim_type(item.get("claim_type"))
        else:
            text = str(item).strip()
            claim_type = ClaimType.uncategorized
        if text:
            results.append((text, claim_type))
    return results


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

        for claim_text, claim_type in _parse_claims(result.content)[:MAX_CLAIMS_PER_CHUNK]:
            score = grounding_score(claim_text, chunk.text)
            claim = KnowledgeClaim(
                owner_id=owner_id,
                source_id=document.id,
                version_id=version_id,
                chunk_id=chunk.id,
                project_id=document.project_id,
                claim_text=claim_text,
                claim_type=claim_type,
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


# --- P3 backfill: retroactive claim_type for pre-existing rows -----------------------------
#
# 2026-07-28 gap found in review: migration 0018 left every EXISTING knowledge_claims row at
# claim_type=uncategorized, and _import_one_file's checksum-idempotency check reports an
# already-`indexed` document as "duplicate" without ever calling extract_claims_for_document
# again (see app/rag/library_import.py) — so a document imported before this PR would never
# organically get typed claims. Re-running extract_claims_for_document is NOT the fix: it
# creates NEW KnowledgeClaim rows, which would duplicate every already-extracted claim. This
# backfill instead UPDATES claim_type in place on existing rows — same id, same source_id/
# version_id/chunk_id/project_id/status/confidence/grounding_score, same relationships,
# nothing else touched.

BACKFILL_BATCH_SIZE = 20

CLAIM_TYPE_BACKFILL_SYSTEM_PROMPT = (
    "Du klassificerar redan extraherade sakpåståenden. Du får en JSON-lista av strängar, ett "
    "påstående per sträng. Svara ENDAST med en JSON-lista av EXAKT SAMMA LÄNGD, i SAMMA "
    "ORDNING — en klassificering per påstående, på formen en sträng ur: idea, decision, "
    "task_reference, vision, technical, historical, uncategorized. Använd uncategorized om du "
    "är osäker, gissa aldrig. Inget annat i svaret, ingen förklaring."
)


@dataclass
class ClaimTypeBackfillResult:
    candidates_total: int = 0
    typed: int = 0
    still_uncategorized: int = 0
    failed: int = 0


def _parse_backfill_types(raw: str, expected_count: int) -> list[ClaimType] | None:
    """Returns None (never raises) when the response can't be trusted to line up 1:1 with
    the batch it was asked to classify — a length mismatch is exactly as unusable as
    unparseable JSON here, since a caller has no reliable way to know WHICH claim a given
    entry in a shorter/longer list was meant for. The whole batch is then left untouched and
    retried on the next backfill run (see backfill_claim_types), never partially guessed."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) != expected_count:
        return None
    return [_coerce_claim_type(item) for item in parsed]


async def backfill_claim_types(
    db: Session,
    owner_id: uuid.UUID,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
    max_batches: int | None = None,
) -> ClaimTypeBackfillResult:
    """Idempotent, resumable retroactive classification for KnowledgeClaim rows created
    before P3 (or by any provider that ignored the typed contract): reclassifies claim_type
    IN PLACE, never creates a row, never touches claim_text/source_id/version_id/chunk_id/
    project_id/status/confidence/grounding_score or any claim_relationships row.

    Candidate filter is deliberately narrow: `claim_type == uncategorized AND
    extraction_version != EXTRACTION_VERSION`. A row already classified as uncategorized BY
    THIS backfill (or by the current typed extraction pipeline) has its extraction_version
    bumped to EXTRACTION_VERSION regardless of the resulting type — that's what makes a
    genuinely-ambiguous claim (the model's honest "uncategorized" answer) settle instead of
    being re-queried forever, and is exactly why re-running this twice in a row is a no-op:
    nothing left matches the filter. A row left untouched by a provider failure keeps its OLD
    extraction_version, so it's still a candidate next run — safely retryable, not silently
    abandoned.

    The caller's session must already have app.current_user_id set to owner_id (same
    precondition as extract_claims_for_document) — this only ever runs within one owner's RLS
    scope, one owner at a time, matching how every admin-triggered maintenance job in this
    codebase already works (see app/routers/admin.py's cleanup endpoint).

    A failed batch (provider error, or a response that can't be trusted to line up 1:1 — see
    _parse_backfill_types) is deliberately excluded from the candidate query for the REST OF
    THIS CALL (via failed_ids below) — a failed row keeps its old extraction_version so it's
    still a real candidate on the NEXT separate backfill_claim_types call, but without this
    exclusion the very same still-uncategorized, still-old-version rows would be re-queried
    and re-fail every single loop iteration, forever, since nothing about them ever changes
    within one run. max_batches=None (the default) means "process every real candidate," not
    "retry the same failing batch unboundedly." """
    result = ClaimTypeBackfillResult()
    batches_done = 0
    failed_ids: set[uuid.UUID] = set()

    while max_batches is None or batches_done < max_batches:
        query = db.query(KnowledgeClaim).filter(
            KnowledgeClaim.owner_id == owner_id,
            KnowledgeClaim.claim_type == ClaimType.uncategorized,
            KnowledgeClaim.extraction_version != EXTRACTION_VERSION,
        )
        if failed_ids:
            query = query.filter(KnowledgeClaim.id.notin_(failed_ids))
        candidates = query.order_by(KnowledgeClaim.created_at.asc()).limit(batch_size).all()
        if not candidates:
            break

        result.candidates_total += len(candidates)
        messages = [
            Message(role="system", content=CLAIM_TYPE_BACKFILL_SYSTEM_PROMPT),
            Message(role="user", content=json.dumps([c.claim_text for c in candidates])),
        ]
        try:
            chat_result, _ = await chat_with_fallback(db, messages)
        except ProviderError:
            result.failed += len(candidates)
            failed_ids.update(c.id for c in candidates)
            batches_done += 1
            continue

        types = _parse_backfill_types(chat_result.content, len(candidates))
        if types is None:
            result.failed += len(candidates)
            failed_ids.update(c.id for c in candidates)
            batches_done += 1
            continue

        for claim, claim_type in zip(candidates, types):
            claim.claim_type = claim_type
            claim.extraction_version = EXTRACTION_VERSION
            db.add(claim)
            if claim_type == ClaimType.uncategorized:
                result.still_uncategorized += 1
            else:
                result.typed += 1
        db.commit()
        batches_done += 1

    return result
