"""Live thread context: latest turns, compacted memory, provenance, supersession.

COMPACTION != DELETION. SUMMARY != SOURCE. MEMORY != AUTHORITY.
Old exact messages remain retrievable. An old compacted summary must not override
newer source truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.types import ActiveDecision, ProvenancePointer
from app.models.continuous_conversation import (
    FounderConversationCompaction,
    FounderConversationDecision,
    FounderConversationProvenance,
)
from app.models.conversation import Message, MessageStatus

ACTIVE_TURN_LIMIT = 20

_SHA = re.compile(r"\b([0-9a-f]{40})\b", re.IGNORECASE)
_BRANCH = re.compile(r"\b((?:cursor|codex|claude)/[\w./-]+)\b")
_FILE_ID = re.compile(r"\bfile[_ -]?id[:\s]+([0-9a-f-]{36})\b", re.IGNORECASE)
_DECISION_ID = re.compile(r"\bdecision[_ -]?id[:\s]+([0-9a-f-]{36})\b", re.IGNORECASE)
_PROVIDER_DECISION = re.compile(
    r"\b(?:use|switch to|byt till|anv[aä]nd)\s+provider\s+([A-Za-z0-9_-]+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LiveThreadContext:
    conversation_id: UUID
    owner_id: UUID
    active_messages: tuple[Message, ...]
    compacted: tuple[FounderConversationCompaction, ...]
    active_decisions: tuple[ActiveDecision, ...]
    provenance: tuple[ProvenancePointer, ...]
    original_count: int

    @property
    def prompt_history(self) -> list[Message]:
        return list(self.active_messages)

    def prompt_blocks(self) -> str:
        parts: list[str] = []
        if self.compacted:
            summaries = "\n".join(item.summary_text for item in self.compacted)
            parts.append(
                "COMPACTED MEMORY (not source, not authority — original turns remain retrievable):\n"
                f"{summaries}"
            )
        if self.active_decisions:
            lines = [f"- {item.topic}: {item.statement}" for item in self.active_decisions]
            parts.append("ACTIVE DECISIONS (current source truth; superseded history is preserved):\n" + "\n".join(lines))
        return "\n\n".join(parts)


def load_original_history(
    db: Session,
    *,
    conversation_id: UUID,
    exclude_message_id: UUID | None = None,
) -> list[Message]:
    query = db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.status == MessageStatus.succeeded,
    )
    if exclude_message_id is not None:
        query = query.filter(Message.id != exclude_message_id)
    return query.order_by(Message.created_at.asc(), Message.id.asc()).all()


def retrieve_original_turn(db: Session, *, owner_id: UUID, message_id: UUID) -> Message | None:
    """Exact historical turn. Compaction never deletes this row."""

    message = db.get(Message, message_id)
    if message is None:
        return None
    from app.models.conversation import Conversation

    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.user_id != owner_id:
        return None
    return message


def extract_identifiers(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _SHA.finditer(text):
        found.append(("sha", match.group(1).lower()))
    for match in _BRANCH.finditer(text):
        found.append(("branch", match.group(1)))
    for match in _FILE_ID.finditer(text):
        found.append(("file_id", match.group(1).lower()))
    for match in _DECISION_ID.finditer(text):
        found.append(("decision_id", match.group(1).lower()))
    return found


def _record_provenance(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    message: Message,
) -> list[FounderConversationProvenance]:
    rows: list[FounderConversationProvenance] = []
    for kind, value in extract_identifiers(message.content or ""):
        existing = (
            db.query(FounderConversationProvenance)
            .filter_by(owner_id=owner_id, message_id=message.id, kind=kind, value=value)
            .one_or_none()
        )
        if existing is not None:
            rows.append(existing)
            continue
        row = FounderConversationProvenance(
            id=uuid4(),
            owner_id=owner_id,
            conversation_id=conversation_id,
            message_id=message.id,
            kind=kind,
            value=value,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        rows.append(row)
    return rows


def record_decision_from_text(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    message: Message,
) -> FounderConversationDecision | None:
    match = _PROVIDER_DECISION.search(message.content or "")
    if not match:
        return None
    statement = match.group(0)
    topic = "provider"
    already = (
        db.query(FounderConversationDecision)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, message_id=message.id, topic=topic)
        .one_or_none()
    )
    if already is not None:
        return already
    identifiers = {kind: value for kind, value in extract_identifiers(message.content or "")}
    identifiers["provider"] = match.group(1)
    previous = (
        db.query(FounderConversationDecision)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, topic=topic, superseded=False)
        .order_by(FounderConversationDecision.created_at.desc())
        .all()
    )
    decision = FounderConversationDecision(
        id=uuid4(),
        owner_id=owner_id,
        conversation_id=conversation_id,
        message_id=message.id,
        topic=topic,
        statement=statement,
        identifiers=identifiers,
        superseded=False,
        superseded_by=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(decision)
    db.flush()
    for old in previous:
        if old.id == decision.id:
            continue
        old.superseded = True
        old.superseded_by = decision.id
    db.flush()
    return decision


def _summarize(messages: list[Message]) -> str:
    excerpts = []
    for message in messages:
        text = (message.content or "").replace("\n", " ").strip()
        excerpts.append(f"{message.role.value}: {text[:180]}")
    return "Compacted earlier turns (sources remain). " + " | ".join(excerpts)[:1800]


def ensure_compaction(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    older: list[Message],
) -> list[FounderConversationCompaction]:
    if not older:
        return []
    existing = (
        db.query(FounderConversationCompaction)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id)
        .order_by(FounderConversationCompaction.created_at.asc())
        .all()
    )
    already = {item for row in existing for item in (row.source_message_ids or [])}
    pending = [message for message in older if str(message.id) not in already]
    if not pending:
        return existing
    source_ids = [str(message.id) for message in pending]
    pointers = []
    for message in pending:
        for kind, value in extract_identifiers(message.content or ""):
            pointers.append({"kind": kind, "value": value, "message_id": str(message.id)})
        _record_provenance(db, owner_id=owner_id, conversation_id=conversation_id, message=message)
    row = FounderConversationCompaction(
        id=uuid4(),
        owner_id=owner_id,
        conversation_id=conversation_id,
        summary_text=_summarize(pending),
        source_message_ids=source_ids,
        source_provenance=pointers,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return existing + [row]


def active_decisions(db: Session, *, owner_id: UUID, conversation_id: UUID) -> list[ActiveDecision]:
    rows = (
        db.query(FounderConversationDecision)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, superseded=False)
        .order_by(FounderConversationDecision.created_at.asc())
        .all()
    )
    return [
        ActiveDecision(
            topic=row.topic,
            statement=row.statement,
            decision_id=row.id,
            message_id=row.message_id,
            identifiers=dict(row.identifiers or {}),
            superseded=False,
        )
        for row in rows
    ]


def assemble_live_context(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    exclude_message_id: UUID | None = None,
    active_limit: int = ACTIVE_TURN_LIMIT,
) -> LiveThreadContext:
    history = load_original_history(db, conversation_id=conversation_id, exclude_message_id=exclude_message_id)
    for message in history:
        _record_provenance(db, owner_id=owner_id, conversation_id=conversation_id, message=message)
        record_decision_from_text(db, owner_id=owner_id, conversation_id=conversation_id, message=message)
    if len(history) > active_limit:
        older, latest = history[:-active_limit], history[-active_limit:]
        compacted = ensure_compaction(db, owner_id=owner_id, conversation_id=conversation_id, older=older)
    else:
        latest = history
        compacted = (
            db.query(FounderConversationCompaction)
            .filter_by(owner_id=owner_id, conversation_id=conversation_id)
            .order_by(FounderConversationCompaction.created_at.asc())
            .all()
        )
    pointers = (
        db.query(FounderConversationProvenance)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id)
        .all()
    )
    return LiveThreadContext(
        conversation_id=conversation_id,
        owner_id=owner_id,
        active_messages=tuple(latest),
        compacted=tuple(compacted),
        active_decisions=tuple(active_decisions(db, owner_id=owner_id, conversation_id=conversation_id)),
        provenance=tuple(
            ProvenancePointer(kind=row.kind, value=row.value, message_id=row.message_id, conversation_id=row.conversation_id)
            for row in pointers
        ),
        original_count=len(history),
    )
