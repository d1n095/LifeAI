import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.schemas import ConversationDetailOut, ConversationOut

router = APIRouter(prefix="/api/conversations", tags=["conversations"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)):
    # RLS on `conversations` already scopes this to the current user — no extra filter needed.
    return db.query(Conversation).order_by(Conversation.updated_at.desc()).all()


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(conversation_id: uuid.UUID, db: Session = Depends(get_db)):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Konversationen hittades inte.")
    messages = (
        db.query(Message)
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return ConversationDetailOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=messages,
    )


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Konversationen hittades inte.")
    db.query(Message).filter_by(conversation_id=conversation.id).delete()
    db.delete(conversation)
    db.commit()
    return {"status": "deleted"}
