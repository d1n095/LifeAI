import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.schemas import ConversationDetailOut, ConversationOut

router = APIRouter(prefix="/api/conversations", tags=["conversations"], dependencies=[Depends(require_founder)])


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
        # S1B (migration 0030, docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.9): `created_at`
        # alone is not a total order, so two messages sharing a timestamp could render in
        # either order between two loads of the same transcript. `id` is the SAME deterministic
        # tiebreaker app/rag/message_sequence_backfill.py numbers by and app/rag/
        # account_export.py already exported by — so the transcript, the export and the
        # assigned ordinals agree with each other, instead of this one path disagreeing with
        # the ordinals being written for it. Deliberately still not `ORDER BY sequence_number`:
        # historical rows are NULL until the backfill job has run (this is the EXPAND phase),
        # and read paths switch over only after the VERIFY step — see §4.8's phased plan.
        .order_by(Message.created_at.asc(), Message.id.asc())
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
def delete_conversation(conversation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Konversationen hittades inte.")
    db.query(Message).filter_by(conversation_id=conversation.id).delete()
    db.delete(conversation)
    db.commit()
    return {"status": "deleted"}
