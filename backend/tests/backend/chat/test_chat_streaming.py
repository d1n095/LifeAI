"""Real end-to-end token streaming (Founder Alpha Item 1). Mirrors the discipline already
established for the non-streaming endpoint (test_chat_message_persistence.py's
persistence/failure-boundary coverage, test_chat_with_fallback_central_authorization_boundary.py's
"spy on every provider to prove zero calls" pattern): a streamed reply must persist as exactly
one correct, complete message row on success; must never leave a duplicate or silently-lost row
on any interruption; and must never reach a provider before the exact same central
egress/lease/LOCAL_ONLY_MODE checks the non-streaming path already enforces.
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole, MessageStatus
from app.providers.base import Message, ProviderError, StreamChunk
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _sse_events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def _latest_message(superuser_db, role: MessageRole) -> MessageModel | None:
    return (
        superuser_db.query(MessageModel)
        .filter(MessageModel.role == role)
        .order_by(MessageModel.created_at.desc())
        .first()
    )


def _patch_streaming_chat(monkeypatch, chunks: list[StreamChunk]):
    async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _chat_stream)


# --------------------------------------------------------------------------------- happy path

def test_stream_happy_path_delivers_incremental_deltas_and_persists_one_row(monkeypatch, client, superuser_db):
    _patch_streaming_chat(monkeypatch, [
        StreamChunk(delta="Hej"),
        StreamChunk(delta=" där"),
        StreamChunk(delta="!"),
        StreamChunk(delta="", done=True, usage={"prompt_tokens": 5, "completion_tokens": 3},
                    provider="openai", model="gpt-4o-mini"),
    ])
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "Hej MainAI"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(res.text)
    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["Hej", " där", "!"]
    final = events[-1]
    assert final["done"] is True
    assert final["assistant_status"] == "succeeded"
    assert final["provider"] == "openai"

    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row is not None
    assert row.content == "Hej där!"
    assert row.status == MessageStatus.succeeded
    # Exactly one assistant row for this user message — never duplicated.
    count = (
        superuser_db.query(MessageModel)
        .filter(MessageModel.in_reply_to_id == row.in_reply_to_id, MessageModel.role == MessageRole.assistant)
        .count()
    )
    assert count == 1


def test_stream_reassembled_content_matches_exact_delta_concatenation(monkeypatch, client):
    _patch_streaming_chat(monkeypatch, [
        StreamChunk(delta="A"), StreamChunk(delta="B"), StreamChunk(delta="C"),
        StreamChunk(delta="", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1}),
    ])
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    events = _sse_events(res.text)
    assert "".join(e["delta"] for e in events if "delta" in e) == "ABC"


def test_stream_default_non_streaming_provider_degrades_to_one_chunk(monkeypatch, client, superuser_db):
    """A provider with no chat_stream() override (the LLMProvider base class default) must
    still work through this endpoint — one chunk, then done — never an error."""
    from app.providers.base import ChatResult

    async def _chat(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="hela svaret på en gång", provider="openai", model=model,
                          raw_usage={"prompt_tokens": 2, "completion_tokens": 4})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    events = _sse_events(res.text)
    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["hela svaret på en gång"]
    assert events[-1]["assistant_status"] == "succeeded"
    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row.content == "hela svaret på en gång"


# ------------------------------------------------------------------------------- failure modes

def test_stream_provider_error_mid_stream_persists_failed_not_stuck(monkeypatch, client, superuser_db):
    async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
        yield StreamChunk(delta="par")
        raise ProviderError("modellen kraschade", category="unreachable", provider_request_may_have_left=False)

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _chat_stream)
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    events = _sse_events(res.text)
    assert any(e.get("delta") == "par" for e in events)
    error_event = next(e for e in events if e.get("error"))
    assert error_event["category"] == "unreachable"
    assert error_event["retryable"] is True
    # Same identifiers the non-streaming failure contract carries — a client needs these to
    # target a retry at the user message that was already durably saved.
    row = _latest_message(superuser_db, MessageRole.assistant)
    assert error_event["user_message_id"] == str(row.in_reply_to_id)
    assert error_event["assistant_message_id"] == str(row.id)

    assert row.status == MessageStatus.failed
    assert row.error_category == "unreachable"
    # The never-silently-lost contract: this is NOT one of _claim_reply_attempt's blocked
    # categories, so a retry can proceed immediately rather than being wedged forever.
    assert not str(row.error_category).startswith(("provider_inflight", "inflight:"))


def test_stream_ending_without_done_sentinel_fails_closed(monkeypatch, client, superuser_db):
    async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
        yield StreamChunk(delta="ofullständigt")
        return

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _chat_stream)
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    events = _sse_events(res.text)
    error_event = next(e for e in events if e.get("error"))
    assert error_event["category"] == "unsupported"
    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row.status == MessageStatus.failed


def test_stream_empty_content_fails_closed_as_external_effect_unknown(monkeypatch, client, superuser_db):
    """Mirrors chat_with_fallback()'s effect_guard: a provider that completes with nothing
    usable must not be recorded as a success."""
    async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
        yield StreamChunk(delta="", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _chat_stream)
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    events = _sse_events(res.text)
    error_event = next(e for e in events if e.get("error"))
    assert error_event["category"] == "external_effect_unknown"
    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row.status == MessageStatus.failed


@pytest.mark.asyncio
async def test_stream_invalid_usage_fails_closed_unit(client, superuser_db):
    """Unit-level test of _generate_stream_events()'s usage-validation branch directly, with a
    stub attempt_budget — ChatAttemptBudget.requires_usage is gated behind real billing
    enforcement (reservation/pricing-verification machinery this file otherwise doesn't set
    up), so this exercises the validation logic itself rather than the full billing stack,
    the same way test_chat_with_fallback_central_authorization_boundary.py isolates central
    checks from unrelated subsystems."""
    from app.db import SessionLocal
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.providers.registry import prepare_single_provider_chat
    from app.request_context import current_user_id as current_user_id_var
    from app.routers.chat import _ChatContext, _build_chat_context, _claim_reply_attempt, _generate_stream_events

    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first()
    db = SessionLocal()
    try:
        current_user_id_var.set(str(founder.id))
        db.execute(__import__("sqlalchemy").text("SET LOCAL app.current_user_id = :uid"), {"uid": str(founder.id)})
        user = db.get(User, founder.id)
        conversation = Conversation(title="unit test", user_id=user.id)
        db.add(conversation)
        db.commit()
        user_message = MessageModel(conversation_id=conversation.id, role=MessageRole.user, content="hej")
        db.add(user_message)
        db.commit()
        db.refresh(user_message)

        assistant_row = _claim_reply_attempt(db, conversation=conversation, user_message=user_message, user=user)
        ctx: _ChatContext = await _build_chat_context(db, conversation=conversation, user_message=user_message, user=user)
        provider, model, outbound_messages = await prepare_single_provider_chat(
            db, ctx.messages, owner_id=user.id, purpose="chat", requested_by="test",
        )

        async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
            yield StreamChunk(delta="svar")
            yield StreamChunk(delta="", done=True, usage={"prompt_tokens": -1, "completion_tokens": 0})

        provider.chat_stream = _chat_stream.__get__(provider)

        class _StubBudget:
            requires_usage = True

            def before(self, *a, **k): ...
            def succeeded(self, *a, **k): ...
            def failed(self, *a, **k): ...

        class _FakeRequest:
            async def is_disconnected(self):
                return False

        events = []
        async for raw in _generate_stream_events(
            conversation_id=conversation.id, user_message_id=user_message.id, assistant_row_id=assistant_row.id,
            user_id=user.id, provider=provider, model=model, outbound_messages=outbound_messages,
            attempt_budget=_StubBudget(), ctx=ctx, request=_FakeRequest(),
        ):
            events.append(json.loads(raw.decode().split("data: ", 1)[1]))

        error_event = next(e for e in events if e.get("error"))
        assert error_event["category"] == "external_effect_unknown"

        refreshed = db.get(MessageModel, assistant_row.id)
        assert refreshed.status == MessageStatus.failed
    finally:
        db.rollback()
        db.close()


def test_stream_bounded_buffering_trips_on_runaway_output(monkeypatch, client, superuser_db):
    """A misbehaving provider that never sends `done` must not grow the server-side buffer
    unboundedly — the hard character cap must trip and fail closed."""
    async def _chat_stream(self, messages, model, *, timeout=None, **kwargs):
        while True:
            yield StreamChunk(delta="x" * 10_000)

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _chat_stream)
    monkeypatch.setattr("app.routers.chat._STREAM_HARD_CHAR_LIMIT", 25_000)
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    events = _sse_events(res.text)
    error_event = next(e for e in events if e.get("error"))
    assert error_event["category"] == "unsupported"
    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row.status == MessageStatus.failed
    # Capped well below what an unbounded loop would have produced.
    assert len(row.content) < 100_000


# ---------------------------------------------------------------------------- central security

def test_stream_local_only_mode_makes_zero_provider_calls(monkeypatch, client, superuser_db):
    settings = get_settings()
    monkeypatch.setattr(settings, "local_only_mode", True)

    calls = []

    async def _spy_stream(self, messages, model, *, timeout=None, **kwargs):
        calls.append(self.name)
        yield StreamChunk(delta="should never run", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _spy_chat(self, messages, model, *, timeout=None, **kwargs):
        calls.append(self.name)
        from app.providers.base import ChatResult
        return ChatResult(content="should never run", provider=self.name, model=model)

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _spy_stream)
    monkeypatch.setattr(OpenAIProvider, "chat", _spy_chat)

    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test"}, headers={"X-CSRF-Token": csrf})
    # No stream should even open -- a plain JSON failure body, same contract as the
    # non-streaming endpoint's equivalent denial.
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    assert body["assistant_status"] == "failed"
    assert body["error_category"] == "local_only_mode_no_provider"
    assert calls == []

    row = _latest_message(superuser_db, MessageRole.assistant)
    assert row.status == MessageStatus.failed


def test_stream_requires_authentication(client):
    res = client.post("/api/chat/stream", json={"message": "test"})
    assert res.status_code in (401, 403)


def test_stream_cross_user_conversation_id_never_attaches_to_someone_elses_conversation(
    monkeypatch, client, superuser_db, make_verified_user
):
    """A genuinely-another-user's conversation_id must never let a reply attach to it or leak
    its existence — same contract as the non-streaming chat() endpoint, which this streaming
    path shares _create_or_get_conversation_and_user_message() with verbatim.

    Note on the actual behavior (true for chat() too, pre-dating this streaming work): RLS's
    conversations_isolation policy filters at the ROW level, so `db.get(Conversation, id)` for
    another user's id returns None exactly as it would for a nonexistent id — indistinguishable
    either way, so no 404 is raised; a new conversation is silently created instead. That is
    safe (RLS already prevents any read or write of the other user's row — confirmed below) and
    unrelated to streaming, so this test pins the real contract rather than an assumed one."""
    from app.models.conversation import Conversation

    other_user, _ = make_verified_user()
    other_conversation = Conversation(title="not yours", user_id=other_user.id)
    superuser_db.add(other_conversation)
    superuser_db.commit()
    superuser_db.refresh(other_conversation)
    other_conversation_id = other_conversation.id

    _patch_streaming_chat(monkeypatch, [
        StreamChunk(delta="hej"),
        StreamChunk(delta="", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1}),
    ])
    csrf = _login(client)
    res = client.post("/api/chat/stream", json={"message": "test", "conversation_id": str(other_conversation_id)},
                      headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    events = _sse_events(res.text)
    # A new conversation was created — never the other user's id.
    assert events[-1]["conversation_id"] != str(other_conversation_id)

    # The other user's conversation itself was never touched.
    untouched = superuser_db.get(Conversation, other_conversation_id)
    assert untouched.title == "not yours"
    message_count = (
        superuser_db.query(MessageModel).filter(MessageModel.conversation_id == other_conversation_id).count()
    )
    assert message_count == 0


# ----------------------------------------------------------------------------- retry endpoint

def test_retry_stream_reuses_existing_user_message_no_duplicate(monkeypatch, client, superuser_db):
    _patch_streaming_chat(monkeypatch, [
        StreamChunk(delta="par"),
        StreamChunk(delta="", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1}),
    ])
    csrf = _login(client)
    # First attempt fails. Must still be an async GENERATOR (yield makes it one) even though
    # it never actually yields a chunk before raising — a plain coroutine function here would
    # fail with "'async for' requires an object with __aiter__ method" at the call site, since
    # provider.chat_stream(...) is always consumed with `async for`.
    async def _fail_stream(self, messages, model, *, timeout=None, **kwargs):
        raise ProviderError("down", category="unreachable", provider_request_may_have_left=False)
        yield  # pragma: no cover -- never reached; makes this an async generator function

    monkeypatch.setattr(OpenAIProvider, "chat_stream", _fail_stream)
    res1 = client.post("/api/chat/stream", json={"message": "retry me"}, headers={"X-CSRF-Token": csrf})
    events1 = _sse_events(res1.text)
    user_message_id = events1[-1]["user_message_id"] if "user_message_id" in events1[-1] else None
    # Fetch it from the DB directly since the error event doesn't carry it; the user message
    # was committed regardless of the provider's fate (see _create_or_get_conversation_and_user_message).
    user_row = (
        superuser_db.query(MessageModel)
        .filter(MessageModel.role == MessageRole.user, MessageModel.content == "retry me")
        .order_by(MessageModel.created_at.desc())
        .first()
    )
    assert user_row is not None
    user_message_count_before = (
        superuser_db.query(MessageModel).filter(MessageModel.role == MessageRole.user).count()
    )

    _patch_streaming_chat(monkeypatch, [
        StreamChunk(delta="par"),
        StreamChunk(delta="", done=True, usage={"prompt_tokens": 1, "completion_tokens": 1}),
    ])
    res2 = client.post(f"/api/chat/messages/{user_row.id}/retry/stream", headers={"X-CSRF-Token": csrf})
    assert res2.status_code == 200
    events2 = _sse_events(res2.text)
    assert events2[-1]["assistant_status"] == "succeeded"

    user_message_count_after = (
        superuser_db.query(MessageModel).filter(MessageModel.role == MessageRole.user).count()
    )
    assert user_message_count_after == user_message_count_before

    assistant_rows = (
        superuser_db.query(MessageModel)
        .filter(MessageModel.in_reply_to_id == user_row.id, MessageModel.role == MessageRole.assistant)
        .all()
    )
    assert len(assistant_rows) == 1
    assert assistant_rows[0].status == MessageStatus.succeeded
    assert assistant_rows[0].content == "par"
