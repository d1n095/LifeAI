"""Bounded founder-memory context for the chat prompt.

Only `status="active"` notes are shown (`list_current_founder_memory`'s safe default), newest
first, with a hard cap on both note count and characters. The block is labelled as the
founder's own recorded statements -- never as verified facts about the world -- so the model
cannot turn a preference into an observation, and a superseded or disputed note can never
resurface as current.

Read-only: this module never records, corrects or promotes a note.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.founder_memory.service import list_current_founder_memory

MAX_NOTES = 8
MAX_CHARS = 2000
MAX_NOTE_CHARS = 400


def format_founder_memory_instructions(
    db: Session,
    *,
    owner_id: uuid.UUID,
    limit: int = MAX_NOTES,
    max_chars: int = MAX_CHARS,
) -> str:
    """The prompt block, or "" when the founder has no current notes.

    Fail-closed on an empty/invalid cap rather than silently returning everything: a caller
    that asks for zero notes gets zero notes.
    """

    if limit <= 0 or max_chars <= 0:
        return ""
    notes = list_current_founder_memory(db, owner_id=owner_id)
    if not notes:
        return ""
    notes = sorted(
        notes,
        key=lambda note: (note.observed_at or note.created_at, str(note.id)),
        reverse=True,
    )[:limit]

    lines: list[str] = []
    used = 0
    for note in notes:
        content = " ".join((note.content or "").split())
        if len(content) > MAX_NOTE_CHARS:
            content = content[: MAX_NOTE_CHARS - 3] + "..."
        line = f"- [{note.note_type}] {content}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return (
        "MINNE (the founder's own recorded statements, not verified facts; "
        "use only when relevant):\n" + "\n".join(lines)
    )
