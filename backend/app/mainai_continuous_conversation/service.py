"""Persistence for the continuous founder↔MainAI conversation.

Canonical thread binding + append-only turn events. Never grants security permissions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.orchestrate import handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.types import ConversationTurnResult
from app.models.conversation import Conversation
from app.models.continuous_conversation import FounderCanonicalConversation, FounderConversationEvent

CANONICAL_TITLE = "MainAI continuous conversation"


class ContinuousConversationError(ValueError):
    pass


def get_or_create_canonical_conversation(db: Session, *, owner_id: UUID) -> Conversation:
    binding = (
        db.query(FounderCanonicalConversation)
        .filter(FounderCanonicalConversation.owner_id == owner_id)
        .one_or_none()
    )
    if binding is not None:
        conversation = db.get(Conversation, binding.conversation_id)
        if conversation is None or conversation.user_id != owner_id:
            raise ContinuousConversationError("canonical conversation binding is broken")
        return conversation
    conversation = Conversation(title=CANONICAL_TITLE, user_id=owner_id)
    db.add(conversation)
    db.flush()
    db.add(
        FounderCanonicalConversation(
            owner_id=owner_id,
            conversation_id=conversation.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    return conversation


def record_event(
    db: Session,
    *,
    owner_id: UUID,
    direction: str,
    kind: str,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    suppressed: bool = False,
    interrupt: bool = False,
    payload: dict | None = None,
    excerpt: str = "",
) -> FounderConversationEvent:
    event = FounderConversationEvent(
        owner_id=owner_id,
        conversation_id=conversation_id,
        message_id=message_id,
        direction=direction,
        kind=kind,
        suppressed=suppressed,
        interrupt=interrupt,
        payload=payload or {},
        excerpt=excerpt[:2000],
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.flush()
    return event


def persist_turn(
    db: Session,
    *,
    owner_id: UUID,
    text: str,
    busy_agents: tuple[str, ...] = (),
    idle_agents: tuple[str, ...] = (),
    draft_outbound: str | None = None,
    message_id: UUID | None = None,
) -> ConversationTurnResult:
    conversation = get_or_create_canonical_conversation(db, owner_id=owner_id)
    result = handle_founder_message(
        text, busy_agents=busy_agents, idle_agents=idle_agents, draft_outbound=draft_outbound
    )
    record_event(
        db,
        owner_id=owner_id,
        conversation_id=conversation.id,
        message_id=message_id,
        direction="inbound",
        kind=result.inbound.kind.value,
        interrupt=result.interrupt_founder,
        payload={
            "relay_categories": [item.value for item in result.inbound.relay_categories],
            "internal_actions": [action.kind.value for action in result.internal_actions],
        },
        excerpt=text,
    )
    if result.outbound is not None:
        record_event(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
            direction="outbound",
            kind=result.outbound.disposition.value,
            suppressed=result.outbound.asks_founder_to_relay,
            excerpt=result.outbound.content,
        )
    for action in result.internal_actions:
        record_event(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
            direction="internal",
            kind=action.kind.value,
            payload={"agent_key": action.agent_key, "detail": action.detail},
            excerpt=action.detail,
        )
    return result


def apply_outbound_filter(content: str) -> str:
    return filter_outbound(content).content
