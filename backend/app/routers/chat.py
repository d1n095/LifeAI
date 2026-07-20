import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.models.conversation import Conversation, Message as MessageModel, MessageRole
from app.models.usage import UsageLog
from app.models.user import User
from app.providers.base import Message, ProviderError
from app.providers.pricing import estimate_cost
from app.providers.registry import chat_with_fallback
from app.rag.retrieve import retrieve_context
from app.rag.trust import assess_confidence, build_trust_instructions, detect_conflicts
from app.schemas import ChatMessageIn, ChatMessageOut, SourceRef

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_founder)])

SYSTEM_PROMPT = (
    "Du är MainAI, grundarens Founder AI — inte en delad eller allmän assistent. Svara "
    "utifrån den kontext som ges nedan från kunskapsbiblioteket. Svara på samma språk som "
    "grundaren skriver på."
)

settings = get_settings()


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

    hits = await retrieve_context(db, user.id, payload.message, top_k=5)
    # Deleted sources can never appear here at all — app/rag/vector_store.py's search()
    # excludes Document.deleted_at IS NOT NULL at the query level, not just in the UI.
    conflicting_pairs = detect_conflicts(db, user.id, [h["document_id"] for h in hits])
    trust = assess_confidence(hits, conflicting_pairs)

    def _label(h: dict) -> str:
        status = h.get("active_truth_status")
        return f"[{h['title']}] (status: {status})" if status and status != "active" else f"[{h['title']}]"

    context_block = "\n\n".join(f"{_label(h)}\n{h['text']}" for h in hits) or "Ingen relevant kunskap hittades."

    history = (
        db.query(MessageModel)
        .filter_by(conversation_id=conversation.id)
        .order_by(MessageModel.created_at.asc())
        .limit(20)
        .all()
    )

    system_content = (
        f"{SYSTEM_PROMPT}\n\nKONTEXT:\n{context_block}\n\n"
        f"TILLFÖRLITLIGHETSINSTRUKTION:\n"
        f"{build_trust_instructions(trust.level, trust.top_source_status, trust.conflicts_detected)}"
    )
    messages = [Message(role="system", content=system_content)]
    messages += [Message(role=m.role.value, content=m.content) for m in history]
    messages.append(Message(role="user", content=payload.message))

    try:
        result, attempted = await chat_with_fallback(db, messages)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    db.add(MessageModel(conversation_id=conversation.id, role=MessageRole.user, content=payload.message))
    db.add(
        MessageModel(
            conversation_id=conversation.id,
            role=MessageRole.assistant,
            content=result.content,
            provider=result.provider,
            model=result.model,
            source_document_ids=",".join(h["document_id"] for h in hits),
        )
    )
    # Touch updated_at explicitly — adding Message rows doesn't trigger the Conversation
    # row's onupdate, and /api/conversations sorts by this to surface recent activity first.
    conversation.updated_at = datetime.utcnow()
    db.add(conversation)
    db.commit()

    sources = [
        SourceRef(
            document_id=uuid.UUID(h["document_id"]),
            title=h["title"],
            snippet=h["text"][:240],
            score=h["score"],
            active_truth_status=h.get("active_truth_status"),
        )
        for h in hits
    ]
    return ChatMessageOut(
        conversation_id=conversation.id,
        reply=result.content,
        provider=result.provider,
        model=result.model,
        sources=sources,
        confidence=trust.level,
        confidence_score=trust.score,
        providers_attempted=attempted,
        conflicts_detected=trust.conflicts_detected,
    )
