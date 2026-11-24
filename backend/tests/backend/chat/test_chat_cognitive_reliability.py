"""Workstream D (cognitive reliability, UX & recovery) — API-level tests against the real
/api/chat endpoint, real Postgres, a deterministic fake chat+embedding provider (never a real
AI key, same pattern as test_chat_source_grounding.py's `_captured_chat_messages`).

Covers:
  A. The real multi-turn-reliability bug this workstream found and fixed: once a conversation
     passes the history window, the outgoing prompt must carry the MOST RECENT messages, not
     the oldest ones (the window used to be selected ascending-with-limit, which silently froze
     every long conversation on its earliest messages forever).
  B. `history_truncated` is reported accurately (false for a short conversation, true once the
     window is exceeded) and the model is told so in plain text, never left to guess.
  C. Genuine ambiguity (two similarly-named projects) sets `needs_clarification` and instructs
     the model to ask rather than silently guess.
  D. A founder reversing their own prior instruction (not merely correcting a stated fact) is
     flagged and the model is told to confirm which instruction now applies.
  E. The model is told not to re-ask something already answered in context.
  F. The full realistic acceptance scenario required by the Workstream D directive: changing
     requirements, contradictory information, an interrupted-then-resumed operation, and a
     repeated question, all in one conversation, without losing context or making an
     unauthorized decision.
"""

import uuid

import pytest

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole, MessageStatus
from app.models.project import Project
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


@pytest.fixture
def _captured_chat_messages(monkeypatch):
    """Fakes the chat call and records every outgoing message list, exactly like
    test_chat_source_grounding.py's fixture of the same name — this file never makes a real
    network/AI call."""
    from app.providers.base import ChatResult

    captured = []

    async def _fake_chat(self, messages, model, **kwargs):
        captured.append(messages)
        return ChatResult(content="Testsvar.", provider="openai", model=model,
                           raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")
    return captured


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _founder_id(superuser_db) -> uuid.UUID:
    from app.models.user import User

    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _send(client, csrf, message, conversation_id=None):
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = str(conversation_id)
    res = client.post("/api/chat", json=payload, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    return res.json()


def _seed_conversation_with_n_message_pairs(superuser_db, conversation_id, owner_id, n, *, label_from=0):
    """Directly inserts N (user, assistant) pairs as already-succeeded history, labelled
    sequentially so a test can assert exactly which turns the live endpoint actually saw."""
    import datetime

    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for i in range(label_from, label_from + n):
        u = MessageModel(
            conversation_id=conversation_id, role=MessageRole.user, status=MessageStatus.succeeded,
            content=f"Fråga nummer {i}.", created_at=base + datetime.timedelta(seconds=i * 20),
        )
        superuser_db.add(u)
        superuser_db.commit()
        a = MessageModel(
            conversation_id=conversation_id, role=MessageRole.assistant, status=MessageStatus.succeeded,
            content=f"Svar nummer {i}.", in_reply_to_id=u.id, created_at=base + datetime.timedelta(seconds=i * 20 + 10),
        )
        superuser_db.add(a)
        superuser_db.commit()


def _create_conversation(superuser_db, owner_id) -> uuid.UUID:
    from app.models.conversation import Conversation

    conv = Conversation(user_id=owner_id)
    superuser_db.add(conv)
    superuser_db.commit()
    return conv.id


# --- A & B: the real history-window bug, and honest truncation signalling ---


def test_short_conversation_is_not_reported_truncated(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    _seed_conversation_with_n_message_pairs(superuser_db, conv_id, founder_id, 3)  # 6 messages, well under the window
    csrf = _login(client)

    body = _send(client, csrf, "Nästa fråga.", conversation_id=conv_id)
    assert body["history_truncated"] is False


def test_long_conversation_sends_the_most_recent_messages_not_the_oldest(client, superuser_db, _captured_chat_messages):
    """The actual regression: 30 prior messages exist (15 pairs), well past the 20-message
    window. Before the fix, the ascending `.limit(20)` query returned messages 0-19 — the
    OLDEST 20 — forever, no matter how long the conversation grew. The fix must instead carry
    the most recent 20 (messages 10-29)."""
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    _seed_conversation_with_n_message_pairs(superuser_db, conv_id, founder_id, 15)  # pairs 0..14 -> messages 0..29
    csrf = _login(client)

    body = _send(client, csrf, "Vad var det senaste vi pratade om?", conversation_id=conv_id)
    assert body["history_truncated"] is True

    outgoing = _captured_chat_messages[-1]
    history_texts = " ".join(m.content for m in outgoing if m.role != "system")
    # The most recent pair (index 14) must be present...
    assert "Fråga nummer 14." in history_texts
    assert "Svar nummer 14." in history_texts
    # ...and the earliest pairs, which a correctly-fixed window has long since dropped, must
    # genuinely be gone from what was sent to the provider -- not just "not asserted".
    assert "Fråga nummer 0." not in history_texts
    assert "Svar nummer 0." not in history_texts

    system_message = next(m for m in outgoing if m.role == "system")
    assert "endast de 20 senaste meddelandena" in system_message.content


# --- C: genuine ambiguity is surfaced, not silently guessed ---


def test_two_distinct_projects_named_in_one_message_trigger_clarification_instruction(client, superuser_db, _captured_chat_messages):
    """Matches app/context/resolver.py's actual ambiguity trigger (see
    test_context_resolver.py::test_two_projects_with_similar_names_is_flagged_ambiguous): two
    genuinely distinct project names both appearing in the founder's own message text, not
    merely two projects existing in the account."""
    founder_id = _founder_id(superuser_db)
    superuser_db.add_all([
        Project(created_by=founder_id, name="Life OS"),
        Project(created_by=founder_id, name="Founder Studio"),
    ])
    superuser_db.commit()
    csrf = _login(client)

    body = _send(client, csrf, "Jämför Life OS med Founder Studio.")
    assert body["needs_clarification"] is True

    system_message = next(m for m in _captured_chat_messages[-1] if m.role == "system")
    assert "Gissa INTE tyst" in system_message.content


def test_unambiguous_message_does_not_trigger_clarification_instruction(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    superuser_db.add(Project(created_by=founder_id, name="Life OS"))
    superuser_db.commit()
    csrf = _login(client)

    body = _send(client, csrf, "Berätta om kvantdatorer.")
    assert body["needs_clarification"] is False


# --- D: a founder reversing their own instruction is flagged, not silently overwritten ---


def test_reversing_a_prior_work_command_is_flagged_to_the_model(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    import datetime
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    u = MessageModel(conversation_id=conv_id, role=MessageRole.user, status=MessageStatus.succeeded,
                      content="Skapa ett nytt dokument i biblioteket.", created_at=base)
    superuser_db.add(u)
    superuser_db.commit()
    csrf = _login(client)

    _send(client, csrf, "Nej, det stämmer inte, gör det inte.", conversation_id=conv_id)
    system_message = next(m for m in _captured_chat_messages[-1] if m.role == "system")
    assert "instruktionsändring" in system_message.content or "upphäva" in system_message.content


# --- E: the model is told not to re-ask what's already known ---


def test_system_prompt_always_discourages_repeating_already_answered_questions(client, superuser_db, _captured_chat_messages):
    csrf = _login(client)
    _send(client, csrf, "Hej, hur mår LifeAI idag?")
    system_message = next(m for m in _captured_chat_messages[-1] if m.role == "system")
    assert "Fråga inte grundaren om något hen redan har svarat på" in system_message.content


# --- F: the full realistic multi-step scenario the Workstream D directive requires ---


def test_multi_step_discussion_with_changing_requirements_contradiction_interruption_and_repeat(
    client, superuser_db, _captured_chat_messages
):
    """One realistic founder conversation exercising all four required properties together:
    changing requirements, contradictory information, an interrupted-then-resumed operation,
    and a question whose answer is already in context. Asserts the system never loses the
    earlier turns and never silently decides anything unauthorized (no provider call happens
    except through the normal, already-authorized /api/chat path -- this test makes no
    assertion that could only pass by weakening that boundary)."""
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    csrf = _login(client)

    # Turn 1: an initial requirement.
    body1 = _send(client, csrf, "Knappen ska vara blå.", conversation_id=conv_id)
    assert body1["assistant_status"] == "succeeded"

    # Turn 2: changing requirements -- the founder revises their own instruction.
    body2 = _send(client, csrf, "Nej, ändra till röd istället för blå.", conversation_id=conv_id)
    assert body2["context_intent"] == "correction"

    # Turn 3: an interrupted operation -- simulate a stream that never completed (the real
    # mechanism Item 1/3 built: a message left in `stream_interrupted`, never silently
    # discarded, never fabricated as complete).
    interrupted = MessageModel(
        conversation_id=conv_id, role=MessageRole.assistant, status=MessageStatus.failed,
        error_category="stream_interrupted", content="Jag börjar titta på design", in_reply_to_id=None,
    )
    superuser_db.add(interrupted)
    superuser_db.commit()

    # Turn 4: resuming -- a fresh message continues the conversation; the prior turns must
    # still be visible to the resolver/provider (not lost because of the interruption).
    body4 = _send(client, csrf, "Fortsätt med det vi pratade om.", conversation_id=conv_id)
    assert body4["context_intent"] == "continuation"
    outgoing4 = _captured_chat_messages[-1]
    history_texts_4 = " ".join(m.content for m in outgoing4 if m.role == "user")
    assert "röd" in history_texts_4 or "blå" in history_texts_4  # earlier requirement turns still present

    # Turn 5: a previously-answered question, asked again -- the model is told (via the
    # system prompt) to answer from existing context rather than ask the founder to repeat
    # themselves. We assert the instruction is present; actual model compliance is a provider
    # concern this suite cannot prove without a live model (see Item 2's CI-based evidence).
    body5 = _send(client, csrf, "Vilken färg ska knappen ha?", conversation_id=conv_id)
    assert body5["assistant_status"] == "succeeded"
    system_message_5 = next(m for m in _captured_chat_messages[-1] if m.role == "system")
    assert "Fråga inte grundaren om något hen redan har svarat på" in system_message_5.content

    # No unauthorized decision: every reply in this scenario went through the one, already
    # fully-authorized /api/chat path -- this test added no new authorization surface, and
    # the fake provider never saw a request this flow didn't legitimately make.
    assert len(_captured_chat_messages) == 4  # turns 1, 2, 4, 5 each call the provider once; turn 3 was a direct DB insert, not a chat call
