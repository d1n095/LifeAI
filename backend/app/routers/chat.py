"""MainAI chat — the user's message and the assistant's reply are two separate, independently
durable steps (see docs/BRANCH_REGISTRY.md's 2026-07-26 LLM Coupling & Failure-Boundary
Audit finding #1): the user's `MessageModel` row is committed BEFORE any provider is ever
called, so a provider failure can never lose or roll back what the founder actually typed.
`_attempt_assistant_reply()` is the one place that generates (or regenerates, on retry) the
assistant's response for an already-persisted user message — it creates or updates exactly
one assistant `MessageModel` row (enforced by migration 0016's partial unique index on
`in_reply_to_id`), never a second, duplicate reply."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.context.resolver import ConversationMessage, resolve_context
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.models.conversation import Conversation, Message as MessageModel, MessageRole, MessageStatus
from app.models.usage import UsageLog
from app.models.user import User
from app.providers.base import Message, ProviderError
from app.providers.pricing import estimate_cost
from app.providers.registry import chat_with_fallback
from app.rag.retrieve import retrieve_context
from app.rag.trust import assess_confidence, build_trust_instructions, detect_claim_conflicts, detect_conflicts
from app.schemas import ChatMessageIn, ChatMessageOut, SourceRef

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_founder)])

SYSTEM_PROMPT = (
    "Du är MainAI, grundarens Founder AI — inte en delad eller allmän assistent. Svara "
    "utifrån den kontext som ges nedan från kunskapsbiblioteket. Svara på samma språk som "
    "grundaren skriver på."
)

settings = get_settings()

# Which of app/providers/base.py's ProviderError.category values a founder can reasonably
# expect to work by simply trying again (transient), vs. ones that need a real config change
# first (a bad/missing key, or a provider that structurally doesn't support this role).
_RETRYABLE_ERROR_CATEGORIES = {"unreachable", "rate_limited"}


def _get_or_create_reply_slot(db: Session, user_message_id: uuid.UUID) -> MessageModel | None:
    return db.query(MessageModel).filter_by(in_reply_to_id=user_message_id).first()


async def _attempt_assistant_reply(db: Session, *, conversation: Conversation, user_message: MessageModel, user: User) -> ChatMessageOut:
    """Generates the assistant's reply for `user_message` (already committed). Used by both
    the initial send and the retry endpoint — a retry runs this exact same path, not a
    parallel one, so its behavior can never drift from the first attempt's."""
    hits = await retrieve_context(db, user.id, user_message.content, top_k=5)
    # Deleted sources can never appear here at all — app/rag/vector_store.py's search()
    # excludes Document.deleted_at IS NOT NULL at the query level, not just in the UI.
    conflicting_pairs = detect_conflicts(db, user.id, [h["document_id"] for h in hits])
    trust = assess_confidence(hits, conflicting_pairs)
    # Claim-level conflicts (STEG 10) can surface a disagreement source-level detection
    # alone would miss — see app/rag/trust.py's detect_claim_conflicts() docstring. ORed in,
    # never replaces the source-level check.
    if not trust.conflicts_detected and detect_claim_conflicts(db, user.id, [h["chunk_id"] for h in hits if h.get("chunk_id")]):
        trust.conflicts_detected = True

    def _label(h: dict) -> str:
        status = h.get("active_truth_status")
        return f"[{h['title']}] (status: {status})" if status and status != "active" else f"[{h['title']}]"

    context_block = "\n\n".join(f"{_label(h)}\n{h['text']}" for h in hits) or "Ingen relevant kunskap hittades."

    history = (
        db.query(MessageModel)
        .filter(
            MessageModel.conversation_id == conversation.id,
            MessageModel.id != user_message.id,
            MessageModel.status == MessageStatus.succeeded,
        )
        .order_by(MessageModel.created_at.asc())
        .limit(20)
        .all()
    )

    # Conversation Context Resolver v1 (see app/context/resolver.py, docs/CONTEXT_RESOLVER_V1.md)
    # — a rule-based classification of what kind of turn this message is (continuation, new
    # topic, correction, ...). Purely observational: it doesn't yet change retrieval or the
    # system prompt — surfaced on the response so the founder and a future UI can see it and
    # build on it without this endpoint's core behavior depending on it.
    context_resolution = resolve_context(
        user_message.content, [ConversationMessage(role=m.role.value, content=m.content, created_at=m.created_at) for m in history]
    )

    system_content = (
        f"{SYSTEM_PROMPT}\n\nKONTEXT:\n{context_block}\n\n"
        f"TILLFÖRLITLIGHETSINSTRUKTION:\n"
        f"{build_trust_instructions(trust.level, trust.top_source_status, trust.conflicts_detected)}"
    )
    messages = [Message(role="system", content=system_content)]
    messages += [Message(role=m.role.value, content=m.content) for m in history]
    messages.append(Message(role="user", content=user_message.content))

    assistant_row = _get_or_create_reply_slot(db, user_message.id)

    try:
        result, attempted = await chat_with_fallback(db, messages)
    except ProviderError as exc:
        category = exc.category or "unreachable"
        if assistant_row is None:
            assistant_row = MessageModel(
                conversation_id=conversation.id,
                role=MessageRole.assistant,
                content="",
                in_reply_to_id=user_message.id,
                status=MessageStatus.failed,
                error_category=category,
            )
            db.add(assistant_row)
        else:
            assistant_row.status = MessageStatus.failed
            assistant_row.error_category = category
            assistant_row.content = ""
        db.commit()
        db.refresh(assistant_row)
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="failed",
            assistant_message_id=assistant_row.id,
            error_category=category,
            error_message=str(exc),
            retryable=category in _RETRYABLE_ERROR_CATEGORIES,
        )

    usage = result.raw_usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost = estimate_cost(result.provider, result.model, prompt_tokens, completion_tokens)
    db.add(
        UsageLog(
            user_id=user.id,
            conversation_id=conversation.id,
            role="chat",
            provider=result.provider,
            model=result.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
    )

    if assistant_row is None:
        assistant_row = MessageModel(conversation_id=conversation.id, role=MessageRole.assistant, in_reply_to_id=user_message.id)
        db.add(assistant_row)
    assistant_row.content = result.content
    assistant_row.provider = result.provider
    assistant_row.model = result.model
    assistant_row.source_document_ids = ",".join(h["document_id"] for h in hits)
    assistant_row.status = MessageStatus.succeeded
    assistant_row.error_category = None

    # Touch updated_at explicitly — adding Message rows doesn't trigger the Conversation
    # row's onupdate, and /api/conversations sorts by this to surface recent activity first.
    conversation.updated_at = datetime.utcnow()
    db.add(conversation)
    db.commit()
    db.refresh(assistant_row)

    sources = [
        SourceRef(
            document_id=uuid.UUID(h["document_id"]),
            title=h["title"],
            snippet=h["text"][:240],
            score=h["score"],
            active_truth_status=h.get("active_truth_status"),
            start_seconds=h.get("start_seconds"),
            end_seconds=h.get("end_seconds"),
        )
        for h in hits
    ]
    return ChatMessageOut(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_status="succeeded",
        assistant_message_id=assistant_row.id,
        reply=result.content,
        provider=result.provider,
        model=result.model,
        sources=sources,
        confidence=trust.level,
        confidence_score=trust.score,
        providers_attempted=attempted,
        conflicts_detected=trust.conflicts_detected,
        context_intent=context_resolution.intent,
        context_confidence=context_resolution.confidence,
    )


@router.post("", response_model=ChatMessageOut)
@limiter.limit(f"{settings.rate_limit_chat_per_minute}/minute")
async def chat(
    request: Request,
    payload: ChatMessageIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    conversation = None
    if payload.conversation_id:
        # RLS on `conversations` already prevents cross-user reads, but we check
        # explicitly too so a mismatched id gives a clean 404 instead of silently
        # falling through to "create a new conversation".
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is not None and conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail="Konversationen hittades inte.")
    if conversation is None:
        conversation = Conversation(title=payload.message[:60], user_id=user.id)
        db.add(conversation)
        db.commit()
        # No db.refresh(): id/created_at/updated_at are populated client-side by SQLAlchemy
        # at flush (see app/models/conversation.py defaults), so the object is already
        # fully populated — a refresh would just be an unnecessary extra round-trip.

    # THE FIX: persisted and committed on its own, before any provider is ever called. If
    # everything after this point fails, the founder's message is still safely in the
    # database — see this module's docstring and docs/BRANCH_REGISTRY.md's audit finding #1.
    user_message = MessageModel(conversation_id=conversation.id, role=MessageRole.user, content=payload.message)
    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    return await _attempt_assistant_reply(db, conversation=conversation, user_message=user_message, user=user)


@router.post("/messages/{message_id}/retry", response_model=ChatMessageOut)
@limiter.limit(f"{settings.rate_limit_chat_per_minute}/minute")
async def retry_assistant_reply(
    request: Request,
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Re-attempts generating the assistant's reply for an already-saved user message —
    never creates a second user message, never duplicates the assistant reply (migration
    0016's partial unique index on `in_reply_to_id` makes a second row impossible even if
    this handler had a bug). Idempotent: if a reply already succeeded, it's returned as-is
    without spending a new provider call or risking a different, non-reproducible answer."""
    user_message = db.get(MessageModel, message_id)
    if user_message is None or user_message.role != MessageRole.user:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")
    conversation = db.get(Conversation, user_message.conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail="Meddelandet hittades inte.")

    existing = _get_or_create_reply_slot(db, user_message.id)
    if existing is not None and existing.status == MessageStatus.succeeded:
        return ChatMessageOut(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_status="succeeded",
            assistant_message_id=existing.id,
            reply=existing.content,
            provider=existing.provider,
            model=existing.model,
        )

    return await _attempt_assistant_reply(db, conversation=conversation, user_message=user_message, user=user)
