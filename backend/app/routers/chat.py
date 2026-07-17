import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.conversation import Conversation, Message as MessageModel, MessageRole
from app.providers.base import Message
from app.providers.registry import resolve_active
from app.rag.retrieve import retrieve_context
from app.schemas import ChatMessageIn, ChatMessageOut, SourceRef

router = APIRouter(prefix="/api/chat", tags=["chat"])

SYSTEM_PROMPT = (
    "Du är MainAI, den centrala AI-assistenten för företaget. Svara utifrån den kontext "
    "som ges nedan från företagets kunskapsbibliotek. Om kontexten inte räcker, säg det "
    "tydligt istället för att gissa. Svara på samma språk som användaren skriver på."
)


@router.post("", response_model=ChatMessageOut)
async def chat(payload: ChatMessageIn, db: Session = Depends(get_db)):
    conversation = None
    if payload.conversation_id:
        conversation = db.get(Conversation, payload.conversation_id)
    if conversation is None:
        conversation = Conversation(title=payload.message[:60])
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    hits = await retrieve_context(db, payload.message, top_k=5)
    context_block = "\n\n".join(f"[{h['title']}]\n{h['text']}" for h in hits) or "Ingen relevant kunskap hittades."

    history = (
        db.query(MessageModel)
        .filter_by(conversation_id=conversation.id)
        .order_by(MessageModel.created_at.asc())
        .limit(20)
        .all()
    )

    messages = [Message(role="system", content=f"{SYSTEM_PROMPT}\n\nKONTEXT:\n{context_block}")]
    messages += [Message(role=m.role.value, content=m.content) for m in history]
    messages.append(Message(role="user", content=payload.message))

    provider, model = resolve_active(db, role="chat")
    result = await provider.chat(messages, model=model)

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
    db.commit()

    sources = [
        SourceRef(document_id=uuid.UUID(h["document_id"]), title=h["title"], snippet=h["text"][:240], score=h["score"])
        for h in hits
    ]
    return ChatMessageOut(
        conversation_id=conversation.id,
        reply=result.content,
        provider=result.provider,
        model=result.model,
        sources=sources,
    )
