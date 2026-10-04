"""Persistence for the continuous founder↔MainAI conversation.

Canonical thread binding + append-only turn events. Bindings and events are
owner-scoped: conversation + owner are bound together. Occupancy is taken from
authoritative task/session state, never from caller-supplied busy/idle lists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.capability import unknown_lookup_reply
from app.mainai_continuous_conversation.occupancy import occupancy_snapshot
from app.mainai_continuous_conversation.orchestrate import handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.types import ConversationTurnResult, OccupancySnapshot, SoftwareTruth
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
        if binding.owner_id != conversation.user_id:
            raise ContinuousConversationError("canonical conversation owner does not match conversation owner")
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
    if conversation_id is not None:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != owner_id:
            raise ContinuousConversationError("event conversation is not owned by this owner")
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
    software_truth: SoftwareTruth | None = None,
    occupancy: OccupancySnapshot | None = None,
) -> ConversationTurnResult:
    conversation = get_or_create_canonical_conversation(db, owner_id=owner_id)
    snapshot = occupancy if occupancy is not None else occupancy_snapshot(db, owner_id=owner_id)
    result = handle_founder_message(
        text,
        occupancy=snapshot,
        draft_outbound=draft_outbound,
        software_truth=software_truth,
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
            "occupancy": {
                "source": snapshot.source,
                "authoritative": snapshot.authoritative,
                "agents": [
                    {
                        "agent_key": item.agent_key,
                        "state": item.state.value,
                        "assignment_id": str(item.assignment_id) if item.assignment_id else None,
                        "task_id": str(item.task_id) if item.task_id else None,
                        "execution_id": str(item.execution_id) if item.execution_id else None,
                    }
                    for item in snapshot.observations
                ],
            },
            "software_truth": {
                "entity_key": software_truth.entity_key,
                "branch": software_truth.branch,
                "sha": software_truth.sha,
                "source": software_truth.source,
                "state": software_truth.state,
            }
            if software_truth is not None
            else None,
            "caller_supplied_occupancy_ignored": bool(busy_agents or idle_agents),
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


def apply_outbound_filter(content: str, *, discovered: SoftwareTruth | None = None) -> str:
    return filter_outbound(content, discovered=discovered).content


def compose_founder_reply(*, draft: str, discovered: SoftwareTruth | None) -> str:
    if discovered is not None and discovered.sha:
        filtered = filter_outbound(draft, discovered=discovered)
        if filtered.asks_founder_to_relay or discovered.sha not in draft:
            return discovered.founder_answer or draft
        return filtered.content
    if discovered is not None and discovered.entity_key != "unspecified" and not discovered.sha:
        filtered = filter_outbound(draft, discovered=discovered)
        if filtered.asks_founder_to_relay:
            return unknown_lookup_reply(discovered.entity_key)
    return apply_outbound_filter(draft, discovered=discovered)


def is_canonical_conversation(db: Session, *, owner_id: UUID, conversation_id: UUID) -> bool:
    binding = (
        db.query(FounderCanonicalConversation)
        .filter(FounderCanonicalConversation.owner_id == owner_id, FounderCanonicalConversation.conversation_id == conversation_id)
        .one_or_none()
    )
    return binding is not None
