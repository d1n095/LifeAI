"""Founder Alpha gap A1-a: message provenance is captured AT WRITE TIME.

Previously `message_source_units` existed only after a founder-triggered backfill job;
the immutable provenance layer lagged the conversation. The chat router now calls the
EXISTING race-safe find-or-create service (app/rag/message_source.py) at every message
commit — user, assistant-success and assistant-failure — fail-open for chat delivery,
with the backfill job remaining the idempotent safety net.
"""

import uuid
import warnings
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.conversation  # noqa: F401
import app.models.memory_source_unit  # noqa: F401
import app.models.user  # noqa: F401
from app.models.conversation import Conversation, Message as MessageModel, MessageRole
from app.models.memory_source_unit import MemorySourceUnit, MessageSourceUnit
from app.models.user import User, UserRole
from app.rag.backfill.message_source import backfill_message_source_units
from app.routers.chat import _capture_message_source

warnings.filterwarnings("ignore")


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from app.db import Base

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _user_and_message(db, role=MessageRole.user, content="Hej från grundaren"):
    user = User(email="alpha@x.example", password_hash="h", role=UserRole.founder)
    db.add(user)
    db.commit()
    conversation = Conversation(title="t", user_id=user.id)
    db.add(conversation)
    db.commit()
    message = MessageModel(conversation_id=conversation.id, role=role, content=content)
    db.add(message)
    db.commit()
    db.refresh(message)
    return user, conversation, message


def test_user_message_capture_creates_immutable_source_unit_at_write_time(db):
    _u, conversation, message = _user_and_message(db)
    _capture_message_source(db, message=message, conversation=conversation)
    rows = db.execute(select(MemorySourceUnit)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_identity_key == f"message:{message.id}"
    subtypes = db.execute(select(MessageSourceUnit)).scalars().all()
    assert len(subtypes) == 1 and subtypes[0].message_id == message.id


def test_capture_is_idempotent_same_unit_not_duplicated(db):
    _u, conversation, message = _user_and_message(db)
    _capture_message_source(db, message=message, conversation=conversation)
    _capture_message_source(db, message=message, conversation=conversation)
    assert len(db.execute(select(MemorySourceUnit)).scalars().all()) == 1


def test_assistant_failure_rows_captured_with_their_role(db):
    _u, conversation, message = _user_and_message(db, role=MessageRole.assistant, content="")
    _capture_message_source(db, message=message, conversation=conversation)
    row = db.execute(select(MemorySourceUnit)).scalar_one()
    assert row.source_role == "assistant"


def test_capture_fails_open_chat_delivery_never_breaks(db, monkeypatch):
    """Provenance must never break or delay the conversation: any failure in capture is
    contained (rollback, log) and the chat flow continues — the backfill covers the gap."""
    _u, conversation, message = _user_and_message(db)

    def boom(*_a, **_k):
        raise RuntimeError("simulated capture failure")

    monkeypatch.setattr("app.routers.chat.get_or_create_message_source_unit", boom)
    # Must NOT raise.
    _capture_message_source(db, message=message, conversation=conversation)
    assert len(db.execute(select(MemorySourceUnit)).scalars().all()) == 0


def test_backfill_still_covering_capture_gaps_is_idempotent(db):
    """The write-time capture and the backfill job must converge on the SAME unit (same
    identity key) — capture never creates a divergent provenance layer."""
    _u, conversation, message = _user_and_message(db)
    _capture_message_source(db, message=message, conversation=conversation)
    result = backfill_message_source_units(db, owner_id=_u.id, batch_size=10)
    assert result.created == 0, "backfill must find nothing new after write-time capture"
    assert len(db.execute(select(MemorySourceUnit)).scalars().all()) == 1


def test_all_three_chat_write_sites_capture_provenance():
    """Structural regression guard: the capture helper must stay wired into the user
    commit, the assistant success commit and the assistant failure commit."""
    import inspect

    from app.routers import chat as chat_module

    src = inspect.getsource(chat_module)
    assert src.count("_capture_message_source(db, message=") == 3, (
        "chat must capture provenance at ALL THREE write sites (user, success, failure)"
    )
