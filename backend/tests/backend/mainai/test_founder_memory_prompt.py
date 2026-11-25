"""Founder memory in the chat prompt.

The block must carry only the current owner's active notes, stay bounded, and never turn a
recorded preference into a claim about verified reality. The chat test proves the wiring: a
note recorded in an earlier session reaches the model's system prompt in a new one.
"""

import uuid

import pytest

from app.config import get_settings
from app.founder_memory import record_founder_memory
from app.founder_memory.prompt import MAX_CHARS, MAX_NOTES, format_founder_memory_instructions
from app.models.founder_memory import FounderMemoryNote
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _founder(db) -> User:
    return db.query(User).filter(User.email == FOUNDER_EMAIL).first()


def _note(db, owner, content: str, *, key: str | None = None, note_type: str = "preference"):
    return record_founder_memory(
        db,
        owner_id=owner.id,
        note_type=note_type,
        content=content,
        idempotency_key=key or f"note-{uuid.uuid4().hex}",
        authority="founder",
        basis="manual",
    )


def test_no_current_notes_produces_no_block(client, superuser_db):
    owner = _founder(superuser_db)
    # The founder is shared across the session's tests; this one asserts the empty state.
    superuser_db.query(FounderMemoryNote).filter_by(owner_id=owner.id).delete()
    superuser_db.commit()
    assert format_founder_memory_instructions(superuser_db, owner_id=owner.id) == ""


def test_block_is_bounded_and_truncates_long_notes(client, superuser_db):
    owner = _founder(superuser_db)
    for index in range(12):
        _note(superuser_db, owner, f"note {index} " + "x" * 600, key=f"cap-{index}")
    superuser_db.commit()

    block = format_founder_memory_instructions(superuser_db, owner_id=owner.id)
    lines = block.splitlines()[1:]
    assert 0 < len(lines) <= MAX_NOTES
    assert len(block) <= MAX_CHARS + 200
    assert all(len(line) <= 420 for line in lines)


def test_another_owners_note_is_never_included(client, superuser_db):
    owner = _founder(superuser_db)
    other = User(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        email_verified=True,
    )
    superuser_db.add(other)
    superuser_db.flush()
    _note(superuser_db, owner, "founder prefers short answers")
    _note(superuser_db, other, "another owner's private note")
    superuser_db.commit()

    block = format_founder_memory_instructions(superuser_db, owner_id=owner.id)
    assert "founder prefers short answers" in block
    assert "another owner's private note" not in block


@pytest.fixture
def captured_provider(monkeypatch):
    captured: list[list] = []

    async def _chat(self, messages, model, **kwargs):
        captured.append(messages)
        return ChatResult(
            content="Kort svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")
    return captured


def test_chat_prompt_carries_the_founders_current_memory(client, superuser_db, captured_provider):
    owner = _founder(superuser_db)
    _note(superuser_db, owner, "Grundaren vill ha korta svar utan sammanfattning.")
    superuser_db.commit()

    login = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert login.status_code == 200, login.text
    response = client.post(
        "/api/chat",
        json={"message": "Hej"},
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert response.status_code == 200, response.text

    system = next(message for message in captured_provider[0] if message.role == "system")
    assert "MINNE" in system.content
    assert "korta svar utan sammanfattning" in system.content
