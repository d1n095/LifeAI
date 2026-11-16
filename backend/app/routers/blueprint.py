"""Blueprint draft API — Founder Alpha item 8/9 wiring.

Exposes the `app.blueprint.service` composition over HTTP so the Founder can, from Life's
interface: request a source-grounded Blueprint draft, inspect its source references, regenerate
a new version, and explicitly approve one. This router adds NO new retrieval/model/archive
system — it reuses:

  * `app.rag.retrieve.retrieve_context` for source-grounding (the exact egress-gated,
    active-embedding-space retrieval chat.py uses), and
  * the shared owner-aware provider boundary (`app.providers.registry.chat_with_fallback`) for
    synthesis — proven end-to-end under a real Ollama model in CI.

Security invariants enforced here (not just in the service):
  * The whole router is `require_founder`-gated; every service call is scoped to the
    authenticated founder's id. RLS (`blueprint_drafts_isolation`) is the second layer.
  * Source identity is NEVER taken from the client. The caller supplies only a free-text
    query; `source_document_ids`/`source_refs` are derived from what retrieval actually
    returned over the founder's own indexed documents. A client cannot smuggle in a
    document id it does not own, nor claim sources that were not retrieved.
  * Approval identity is NEVER taken from the client. `approved_by` is the authenticated
    founder's id from `require_founder`, so a request body cannot forge who approved a
    draft.
  * Generation that is not grounded in at least one retrieved source is refused (422),
    never an ungrounded hallucination presented as a Blueprint.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.blueprint.service import (
    BlueprintError,
    approve_blueprint_draft,
    generate_blueprint_draft,
    get_blueprint_draft,
    list_blueprint_drafts,
)
from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.models.blueprint_draft import BlueprintDraft
from app.models.user import User
from app.providers.base import ProviderError
from app.providers.registry import chat_with_fallback
from app.rag.retrieve import retrieve_context

router = APIRouter(
    prefix="/api/blueprint",
    tags=["blueprint"],
    dependencies=[Depends(require_founder)],
)

# How many retrieved chunks ground a draft. Bounded so a single request cannot be used to
# pull an unbounded amount of source text into one synthesis prompt.
_MAX_TOP_K = 20
_DEFAULT_TOP_K = 8

# Synthesis-prompt bounds. Retrieval may ground a draft in up to _MAX_TOP_K sources (all of
# which are recorded in source_refs for traceability), but the prompt actually sent to the local
# model uses only the most-relevant few, each trimmed — a small local model (e.g. a 0.5B on a CPU
# runner) otherwise takes long enough on a large prompt to exceed the provider timeout. This
# trims the MODEL INPUT only; it never reduces what is recorded as the draft's sources.
_SYNTH_PROMPT_MAX_SOURCES = 6
_SYNTH_EXCERPT_CHARS = 400
# Generous timeout for one local synthesis call: real local generation on CPU is slow, and a
# working-but-slow generation must not be cut off and misreported as "model unavailable".
_SYNTH_TIMEOUT_SECONDS = 240.0


class GenerateDraftIn(BaseModel):
    title: str
    # Free text only. There is deliberately NO source_document_ids field: sources are derived
    # from retrieval, never supplied by the client (see module docstring).
    query: str | None = None
    top_k: int = _DEFAULT_TOP_K

    @field_validator("title")
    @classmethod
    def _non_empty_title(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title must not be empty")
        if len(v) > 300:
            raise ValueError("title too long")
        return v

    @field_validator("query")
    @classmethod
    def _bounded_query(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("top_k")
    @classmethod
    def _bounded_top_k(cls, v: int) -> int:
        if v < 1:
            return 1
        return min(v, _MAX_TOP_K)


class RegenerateDraftIn(BaseModel):
    # Regeneration reuses the existing draft's title (a new version of the same Blueprint);
    # the only thing the caller may steer is the grounding query and breadth.
    query: str | None = None
    top_k: int = _DEFAULT_TOP_K

    @field_validator("query")
    @classmethod
    def _bounded_query(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("top_k")
    @classmethod
    def _bounded_top_k(cls, v: int) -> int:
        if v < 1:
            return 1
        return min(v, _MAX_TOP_K)


class SourceRefOut(BaseModel):
    document_id: str
    similarity: float | None = None
    excerpt: str | None = None


class BlueprintDraftOut(BaseModel):
    # model_provider/model_name are real columns, not pydantic model config — opt out of the
    # protected "model_" namespace so these are plain fields.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    title: str
    version: int
    status: str
    body: str
    generated_from_query: str | None
    source_document_ids: list[str]
    source_refs: list[SourceRefOut]
    model_provider: str | None
    model_name: str | None
    provenance: dict[str, Any]
    created_at: datetime | None
    approved_at: datetime | None
    approved_by: str | None


class BlueprintDraftSummaryOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    title: str
    version: int
    status: str
    source_count: int
    model_provider: str | None
    model_name: str | None
    created_at: datetime | None
    approved_at: datetime | None


def _to_out(row: BlueprintDraft) -> BlueprintDraftOut:
    return BlueprintDraftOut(
        id=str(row.id),
        title=row.title,
        version=row.version,
        status=row.status,
        body=row.body,
        generated_from_query=row.generated_from_query,
        source_document_ids=list(row.source_document_ids or []),
        source_refs=[SourceRefOut(**ref) for ref in (row.source_refs or [])],
        model_provider=row.model_provider,
        model_name=row.model_name,
        provenance=dict(row.provenance or {}),
        created_at=row.created_at,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
    )


def _to_summary(row: BlueprintDraft) -> BlueprintDraftSummaryOut:
    return BlueprintDraftSummaryOut(
        id=str(row.id),
        title=row.title,
        version=row.version,
        status=row.status,
        source_count=len(row.source_document_ids or []),
        model_provider=row.model_provider,
        model_name=row.model_name,
        created_at=row.created_at,
        approved_at=row.approved_at,
    )


def _synthesis_prompt(title: str, query: str | None, hits: list[dict[str, Any]]) -> str:
    """Build the synthesis instruction from the retrieved sources. The model is asked to
    write strictly from the supplied excerpts and to NOT invent facts not present in them —
    the draft is an interpretation OF the originals, not free generation."""
    lines = [
        "You are helping the project's founder draft an initial project Blueprint.",
        f"Blueprint title: {title}",
    ]
    if query:
        lines.append(f"Focus of this draft: {query}")
    lines.append(
        "Write a clear, structured Blueprint draft using ONLY the information in the numbered "
        "sources below. Do not invent facts, dates, names or decisions that are not present in "
        "the sources. Where the sources are silent, say so rather than guessing. This is a "
        "DRAFT for the founder to review and edit — not a final or approved document."
    )
    lines.append("")
    # Use only the most-relevant few sources in the prompt (hits arrive best-first from
    # retrieval), each trimmed — see _SYNTH_PROMPT_MAX_SOURCES/_SYNTH_EXCERPT_CHARS. All grounded
    # sources are still recorded in the draft's source_refs by the service; this bounds only what
    # the local model has to read so generation finishes within the timeout.
    prompt_hits = hits[:_SYNTH_PROMPT_MAX_SOURCES]
    if len(hits) > len(prompt_hits):
        lines.append(f"(Showing the {len(prompt_hits)} most relevant of {len(hits)} grounded sources.)")
    lines.append("Sources:")
    for i, h in enumerate(prompt_hits, start=1):
        excerpt = str(h.get("text") or h.get("excerpt") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] (document {h.get('document_id')}) {excerpt[:_SYNTH_EXCERPT_CHARS]}")
    return "\n".join(lines)


async def _synthesize_with_active_model(
    db: Session, *, owner_id: uuid.UUID, title: str, query: str | None, hits: list[dict[str, Any]]
) -> tuple[str, str | None, str | None]:
    """Call the active local chat model to synthesise the draft body. Returns
    (body, provider_name, model_name). Raises HTTPException(503) when no usable chat model
    is configured or the provider fails — an honest 'model unavailable', never a fabricated
    body."""
    from app.providers.base import Message

    prompt = _synthesis_prompt(title, query, hits)
    messages = [
        Message(
            role="system",
            content=(
                "You draft project Blueprints strictly from provided source excerpts. "
                "You never invent information that is not in the sources."
            ),
        ),
        Message(role="user", content=prompt),
    ]
    try:
        result, _attempted = await chat_with_fallback(
            db,
            messages,
            owner_id=owner_id,
            purpose="blueprint_synthesis",
            requested_by="routers.blueprint._synthesize_with_active_model",
            # Security evidence must survive provider failure, an empty response, and any
            # later rollback of the Blueprint business transaction. The shared provider
            # boundary commits one content-free receipt before each dispatch and fails closed
            # before provider.chat() if that independent commit cannot be made.
            durable_disclosure_receipts=True,
            timeout=_SYNTH_TIMEOUT_SECONDS,
        )
    except EgressDeniedError as exc:
        # The disclosure ledger row is part of the security evidence. No Blueprint row has
        # been created at this point, so committing here persists only the policy decisions
        # already made by retrieval/synthesis. Never retry or fall back after a content denial.
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Källmaterialet får inte skickas till den aktiva modellen enligt egress-policyn.",
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Språkmodellen kunde inte generera utkastet just nu.",
        ) from exc

    body = (getattr(result, "content", None) or "").strip()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Språkmodellen returnerade ett tomt utkast.",
        )
    # The ChatResult identifies what actually answered (including a legitimate configured
    # fallback), rather than re-resolving mutable provider configuration after the call.
    provider_name = getattr(result, "provider", None)
    model_name = getattr(result, "model", None)
    return body, provider_name, model_name


async def _generate(db: Session, *, owner_id: uuid.UUID, title: str, query: str | None, top_k: int) -> BlueprintDraft:
    grounding_query = query or title
    try:
        hits = await retrieve_context(db, owner_id, grounding_query, top_k=top_k)
    except EgressDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Frågan kan inte användas för extern inbäddning enligt egress-policyn.",
        ) from exc

    grounded = [h for h in hits if h.get("document_id")]
    if not grounded:
        # No indexed source matched — refuse rather than synthesise an ungrounded draft.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Inga indexerade källor matchade — ett Blueprint-utkast måste vara grundat i "
                "minst en av dina egna indexerade källor."
            ),
        )
    # Carry the retrieval score through under the key the service records for traceability.
    for h in grounded:
        if h.get("similarity") is None and h.get("score") is not None:
            h["similarity"] = h["score"]

    body, provider_name, model_name = await _synthesize_with_active_model(
        db, owner_id=owner_id, title=title, query=query, hits=grounded
    )

    try:
        row = generate_blueprint_draft(
            db,
            owner_id=owner_id,
            title=title,
            source_hits=grounded,
            synthesizer=lambda _t, _q, _h: body,
            query=query,
            model_provider=provider_name,
            model_name=model_name,
        )
    except BlueprintError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return row


@router.post("/drafts", response_model=BlueprintDraftOut, status_code=status.HTTP_201_CREATED)
async def create_draft(
    payload: GenerateDraftIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> BlueprintDraftOut:
    row = await _generate(
        db, owner_id=user.id, title=payload.title, query=payload.query, top_k=payload.top_k
    )
    return _to_out(row)


@router.get("/drafts", response_model=list[BlueprintDraftSummaryOut])
def list_drafts(
    title: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> list[BlueprintDraftSummaryOut]:
    rows = list_blueprint_drafts(db, owner_id=user.id, title=title)
    return [_to_summary(r) for r in rows]


@router.get("/drafts/{draft_id}", response_model=BlueprintDraftOut)
def get_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> BlueprintDraftOut:
    row = get_blueprint_draft(db, owner_id=user.id, draft_id=draft_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint-utkast hittades inte.")
    return _to_out(row)


@router.post("/drafts/{draft_id}/regenerate", response_model=BlueprintDraftOut, status_code=status.HTTP_201_CREATED)
async def regenerate_draft(
    draft_id: uuid.UUID,
    payload: RegenerateDraftIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> BlueprintDraftOut:
    existing = get_blueprint_draft(db, owner_id=user.id, draft_id=draft_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint-utkast hittades inte.")
    # A new, immutable version of the SAME blueprint (same title); the prior version is
    # untouched. Regeneration never promotes or mutates an approved version.
    row = await _generate(
        db, owner_id=user.id, title=existing.title, query=payload.query, top_k=payload.top_k
    )
    return _to_out(row)


@router.post("/drafts/{draft_id}/approve", response_model=BlueprintDraftOut)
def approve_draft(
    draft_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
) -> BlueprintDraftOut:
    # Approval identity comes from the authenticated founder, never from the request body.
    try:
        row = approve_blueprint_draft(
            db, owner_id=user.id, draft_id=draft_id, approved_by=str(user.id)
        )
    except BlueprintError as exc:
        # Not found for this owner, or not in 'draft' status (e.g. already approved).
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _to_out(row)
