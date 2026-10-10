"""Live thread context: latest turns, compacted memory, provenance, supersession.

COMPACTION != DELETION. SUMMARY != SOURCE. MEMORY != AUTHORITY.
Old exact messages remain retrievable. An old compacted summary must not override
newer source-backed decisions. Prompt size stays bounded. New turns do not rescan
the full history.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text, tuple_
from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.types import ActiveDecision, ProvenancePointer
from app.models.continuous_conversation import (
    FounderConversationCheckpoint,
    FounderConversationCompaction,
    FounderConversationDecision,
    FounderConversationProvenance,
)
from app.models.conversation import Message, MessageStatus

ACTIVE_TURN_LIMIT = 20
L0_BATCH = 20
L0_PROMPT_LIMIT = 2
L1_PROMPT_LIMIT = 1
PROMPT_COMPACTION_CHAR_BUDGET = 4000

_SHA = re.compile(r"\b([0-9a-f]{40})\b", re.IGNORECASE)
_BRANCH = re.compile(r"\b((?:cursor|codex|claude)/[\w./-]+)\b")
_FILE_ID = re.compile(r"\bfile[_ -]?id[:\s]+([0-9a-f-]{36})\b", re.IGNORECASE)
_DECISION_ID = re.compile(r"\bdecision[_ -]?id[:\s]+([0-9a-f-]{36})\b", re.IGNORECASE)

_PROVIDER_EXTRACTORS = (
    re.compile(
        r"\b(?:use|switch\s+to|go\s+with|let'?s\s+go\s+with|going\s+with)\s+provider\s+([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bbyt\s+(?:till|leverant[öo]r(?:en)?\s+till)\s+(?:provider\s+)?([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\banv[aä]nd\s+provider\s+([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"\bprovider\s+([A-Za-z0-9_-]+)\s+instead\b", re.IGNORECASE),
)

LAST_INCREMENTAL_SCAN = 0


@dataclass(frozen=True)
class LiveThreadContext:
    conversation_id: UUID
    owner_id: UUID
    active_messages: tuple[Message, ...]
    compacted: tuple[FounderConversationCompaction, ...]
    active_decisions: tuple[ActiveDecision, ...]
    provenance: tuple[ProvenancePointer, ...]
    original_count: int
    messages_scanned: int = 0
    prompt_char_count: int = 0

    @property
    def prompt_history(self) -> list[Message]:
        return list(self.active_messages)

    def prompt_blocks(self) -> str:
        parts: list[str] = []
        prompt_summaries = [item for item in self.compacted if not item.superseded]
        l1 = [item for item in prompt_summaries if item.layer == "l1"][-L1_PROMPT_LIMIT:]
        l0 = [item for item in prompt_summaries if item.layer == "l0"][-L0_PROMPT_LIMIT:]
        selected = l1 + l0
        if selected:
            text = "\n".join(item.summary_text for item in selected)
            if len(text) > PROMPT_COMPACTION_CHAR_BUDGET:
                text = text[:PROMPT_COMPACTION_CHAR_BUDGET] + "…"
            parts.append(
                "COMPACTED MEMORY (not source, not authority — original turns remain retrievable; "
                "active decisions override stale summaries):\n"
                f"{text}"
            )
        if self.active_decisions:
            lines = [
                f"- {item.decision_key or item.topic}: {item.value or item.statement} "
                f"(status={item.status}; effective_at={item.effective_at})"
                for item in self.active_decisions
            ]
            parts.append(
                "ACTIVE DECISIONS (current source truth; superseded history is preserved; "
                "summaries cannot override these):\n" + "\n".join(lines)
            )
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


def extract_provider_decision(text: str) -> tuple[str, str] | None:
    """Structured provider decision. Newer source-backed value wins; phrase regex is not truth."""

    for pattern in _PROVIDER_EXTRACTORS:
        match = pattern.search(text or "")
        if match:
            return match.group(0), match.group(1)
    return None


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
    extracted = extract_provider_decision(message.content or "")
    if not extracted:
        return None
    statement, value = extracted
    decision_key = "provider"
    already = (
        db.query(FounderConversationDecision)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, message_id=message.id, decision_key=decision_key)
        .one_or_none()
    )
    if already is None:
        already = (
            db.query(FounderConversationDecision)
            .filter_by(owner_id=owner_id, conversation_id=conversation_id, message_id=message.id, topic=decision_key)
            .one_or_none()
        )
    if already is not None:
        return already
    identifiers = {kind: ident for kind, ident in extract_identifiers(message.content or "")}
    identifiers["provider"] = value
    effective_at = message.created_at or datetime.now(timezone.utc)
    decision_id = db.execute(
        text(
            "SELECT record_founder_conversation_decision("
            ":owner_id, :conversation_id, :message_id, :decision_key, "
            ":statement, :value, CAST(:identifiers AS jsonb), :effective_at)"
        ),
        {
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "message_id": message.id,
            "decision_key": decision_key,
            "statement": statement,
            "value": value,
            "identifiers": json.dumps(identifiers),
            "effective_at": effective_at,
        },
    ).scalar_one()
    db.flush()
    return db.get(FounderConversationDecision, decision_id)


def _summarize(messages: list[Message]) -> str:
    excerpts = []
    for message in messages:
        text = (message.content or "").replace("\n", " ").strip()
        excerpts.append(f"{message.role.value}: {text[:180]}")
    return "Compacted earlier turns (sources remain). " + " | ".join(excerpts)[:1800]


def _load_checkpoint(
    db: Session, *, owner_id: UUID, conversation_id: UUID
) -> FounderConversationCheckpoint | None:
    return db.get(FounderConversationCheckpoint, (owner_id, conversation_id))


def _save_checkpoint(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    last_message: Message | None,
    processed_count: int,
    scanned: int,
) -> FounderConversationCheckpoint:
    row = _load_checkpoint(db, owner_id=owner_id, conversation_id=conversation_id)
    if row is None:
        row = FounderConversationCheckpoint(
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        db.add(row)
    if last_message is not None:
        row.last_processed_message_id = last_message.id
        row.last_processed_created_at = last_message.created_at
    row.last_processed_count = processed_count
    row.messages_scanned = scanned
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return row


def _unprocessed_messages(
    db: Session,
    *,
    conversation_id: UUID,
    exclude_message_id: UUID | None,
    checkpoint: FounderConversationCheckpoint | None,
) -> list[Message]:
    query = db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.status == MessageStatus.succeeded,
    )
    if exclude_message_id is not None:
        query = query.filter(Message.id != exclude_message_id)
    if checkpoint is not None and checkpoint.last_processed_created_at is not None and checkpoint.last_processed_message_id is not None:
        watermark = checkpoint.last_processed_created_at
        if getattr(watermark, "tzinfo", None) is not None:
            watermark = watermark.replace(tzinfo=None)
        query = query.filter(
            tuple_(Message.created_at, Message.id) > tuple_(watermark, checkpoint.last_processed_message_id)
        )
    return query.order_by(Message.created_at.asc(), Message.id.asc()).all()


def _latest_active(
    db: Session,
    *,
    conversation_id: UUID,
    exclude_message_id: UUID | None,
    active_limit: int,
) -> list[Message]:
    query = db.query(Message).filter(
        Message.conversation_id == conversation_id,
        Message.status == MessageStatus.succeeded,
    )
    if exclude_message_id is not None:
        query = query.filter(Message.id != exclude_message_id)
    rows = query.order_by(Message.created_at.desc(), Message.id.desc()).limit(active_limit).all()
    return list(reversed(rows))


def _rollup_l0(db: Session, *, owner_id: UUID, conversation_id: UUID) -> None:
    active_l0 = (
        db.query(FounderConversationCompaction)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, layer="l0", superseded=False)
        .order_by(FounderConversationCompaction.created_at.asc())
        .all()
    )
    if len(active_l0) <= L0_PROMPT_LIMIT + 1:
        return
    to_roll = active_l0[: len(active_l0) - L0_PROMPT_LIMIT]
    source_ids = [item for row in to_roll for item in (row.source_message_ids or [])]
    pointers = [item for row in to_roll for item in (row.source_provenance or [])]
    summary = "Rolled-up compacted memory (not source). " + " || ".join(row.summary_text[:400] for row in to_roll)[:1800]
    rollup = FounderConversationCompaction(
        id=uuid4(),
        owner_id=owner_id,
        conversation_id=conversation_id,
        summary_text=summary,
        source_message_ids=source_ids,
        source_provenance=pointers,
        layer="l1",
        superseded=False,
        covering_through_message_id=to_roll[-1].covering_through_message_id,
        covering_through_created_at=to_roll[-1].covering_through_created_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(rollup)
    db.flush()
    for row in to_roll:
        row.superseded = True
        row.superseded_by = rollup.id
    older_l1 = (
        db.query(FounderConversationCompaction)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, layer="l1", superseded=False)
        .order_by(FounderConversationCompaction.created_at.asc())
        .all()
    )
    if len(older_l1) > L1_PROMPT_LIMIT:
        keep = older_l1[-L1_PROMPT_LIMIT:]
        for row in older_l1:
            if row.id not in {item.id for item in keep}:
                row.superseded = True
                row.superseded_by = keep[-1].id
    db.flush()


def ensure_compaction(
    db: Session,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    older: list[Message],
) -> list[FounderConversationCompaction]:
    if not older:
        return (
            db.query(FounderConversationCompaction)
            .filter_by(owner_id=owner_id, conversation_id=conversation_id)
            .order_by(FounderConversationCompaction.created_at.asc())
            .all()
        )
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
    for start in range(0, len(pending), L0_BATCH):
        batch = pending[start : start + L0_BATCH]
        source_ids = [str(message.id) for message in batch]
        pointers = []
        for message in batch:
            for kind, value in extract_identifiers(message.content or ""):
                pointers.append({"kind": kind, "value": value, "message_id": str(message.id)})
        row = FounderConversationCompaction(
            id=uuid4(),
            owner_id=owner_id,
            conversation_id=conversation_id,
            summary_text=_summarize(batch),
            source_message_ids=source_ids,
            source_provenance=pointers,
            layer="l0",
            superseded=False,
            covering_through_message_id=batch[-1].id,
            covering_through_created_at=batch[-1].created_at,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        existing.append(row)
    db.flush()
    _rollup_l0(db, owner_id=owner_id, conversation_id=conversation_id)
    return (
        db.query(FounderConversationCompaction)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id)
        .order_by(FounderConversationCompaction.created_at.asc())
        .all()
    )


def active_decisions(db: Session, *, owner_id: UUID, conversation_id: UUID) -> list[ActiveDecision]:
    rows = (
        db.query(FounderConversationDecision)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id, status="active")
        .order_by(FounderConversationDecision.effective_at.asc(), FounderConversationDecision.created_at.asc())
        .all()
    )
    if not rows:
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
            decision_key=row.decision_key or row.topic,
            value=row.value or row.statement,
            status=row.status or "active",
            effective_at=row.effective_at or row.created_at,
            source_turn_id=row.source_turn_id or row.message_id,
            supersedes=row.superseded_by,
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
    global LAST_INCREMENTAL_SCAN
    checkpoint = _load_checkpoint(db, owner_id=owner_id, conversation_id=conversation_id)
    pending = _unprocessed_messages(
        db,
        conversation_id=conversation_id,
        exclude_message_id=exclude_message_id,
        checkpoint=checkpoint,
    )
    LAST_INCREMENTAL_SCAN = len(pending)
    for message in pending:
        _record_provenance(db, owner_id=owner_id, conversation_id=conversation_id, message=message)
        record_decision_from_text(db, owner_id=owner_id, conversation_id=conversation_id, message=message)
    latest = _latest_active(
        db,
        conversation_id=conversation_id,
        exclude_message_id=exclude_message_id,
        active_limit=active_limit,
    )
    latest_ids = {message.id for message in latest}
    older = [message for message in pending if message.id not in latest_ids]
    compacted = ensure_compaction(db, owner_id=owner_id, conversation_id=conversation_id, older=older)
    original_count = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.status == MessageStatus.succeeded,
        )
        .count()
    )
    if exclude_message_id is not None:
        original_count = max(0, original_count - 1)
    last_seen = pending[-1] if pending else None
    processed_count = (checkpoint.last_processed_count if checkpoint is not None else 0) + len(pending)
    _save_checkpoint(
        db,
        owner_id=owner_id,
        conversation_id=conversation_id,
        last_message=last_seen,
        processed_count=processed_count,
        scanned=len(pending),
    )
    pointers = (
        db.query(FounderConversationProvenance)
        .filter_by(owner_id=owner_id, conversation_id=conversation_id)
        .all()
    )
    decisions = tuple(active_decisions(db, owner_id=owner_id, conversation_id=conversation_id))
    ctx = LiveThreadContext(
        conversation_id=conversation_id,
        owner_id=owner_id,
        active_messages=tuple(latest),
        compacted=tuple(compacted),
        active_decisions=decisions,
        provenance=tuple(
            ProvenancePointer(kind=row.kind, value=row.value, message_id=row.message_id, conversation_id=row.conversation_id)
            for row in pointers
        ),
        original_count=original_count,
        messages_scanned=len(pending),
    )
    return LiveThreadContext(
        conversation_id=ctx.conversation_id,
        owner_id=ctx.owner_id,
        active_messages=ctx.active_messages,
        compacted=ctx.compacted,
        active_decisions=ctx.active_decisions,
        provenance=ctx.provenance,
        original_count=ctx.original_count,
        messages_scanned=ctx.messages_scanned,
        prompt_char_count=len(ctx.prompt_blocks()),
    )
