"""Phase A (Real Conversational Intelligence) — API-level tests against the real /api/chat
endpoint, real Postgres, a deterministic fake chat+embedding provider (same pattern as
test_chat_cognitive_reliability.py's `_captured_chat_messages`; never a real AI key).

Gap assessment (see this phase's dispatch report for the full version): most of the required
conversational-intelligence properties were already correctly implemented by earlier rounds —
cross-conversation memory (Workstream A), the history-window fix + ambiguity/instruction-
reversal/repeated-question instructions (Workstream D). This file covers what was genuinely
still missing:

  A. Self-consistency: the model is told not to silently contradict its own earlier answer in
     the same conversation (distinct from "don't re-ask the founder", which already existed).
  B. The founder's own critical behavioral objective: understand the underlying GOAL behind a
     suggestion, not just execute the literal wording — gated to turns that actually state a
     suggestion (a work command or an idea), not spammed on every ordinary message.
  C. A realistic long-conversation (30+ messages) regression exercising reference resolution,
     repeated-question avoidance and goal-understanding together, continuing straight from the
     Workstream D acceptance scenario rather than duplicating it.

Explicit limitation, same honesty standard as every file in this effort: these tests assert
that the correct instruction text reaches the outgoing prompt and that the deterministic
resolver signal fires correctly. They do NOT and cannot prove a live model actually complies
with the instruction — that would need a real provider (see Item 2's GitHub Actions evidence
for the one place in this whole integration effort that has ever exercised a genuinely live
model).
"""

import datetime
import uuid

import pytest

from app.config import get_settings
from app.models.conversation import Conversation, Message as MessageModel, MessageRole, MessageStatus
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


@pytest.fixture
def _captured_chat_messages(monkeypatch):
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


def _create_conversation(superuser_db, owner_id) -> uuid.UUID:
    conv = Conversation(user_id=owner_id)
    superuser_db.add(conv)
    superuser_db.commit()
    return conv.id


def _system_message(captured_batch):
    return next(m for m in captured_batch if m.role == "system")


# --- A: self-consistency with the assistant's own earlier answer ---


def test_first_turn_has_no_self_consistency_instruction(client, superuser_db, _captured_chat_messages):
    """With no prior history at all, there is nothing to be consistent WITH yet — the
    instruction should not appear (it would be noise, and its presence/absence is itself a
    meaningful signal worth asserting both ways)."""
    csrf = _login(client)
    _send(client, csrf, "Hej, vad kan du hjälpa mig med?")
    system_message = _system_message(_captured_chat_messages[-1])
    assert "var konsekvent med det svaret" not in system_message.content


def test_second_turn_carries_self_consistency_instruction(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    csrf = _login(client)

    _send(client, csrf, "Vad är klockan ungefär i Sverige nu?", conversation_id=conv_id)
    body2 = _send(client, csrf, "Och vad sa du om det igen?", conversation_id=conv_id)
    assert body2["assistant_status"] == "succeeded"

    system_message = _system_message(_captured_chat_messages[-1])
    assert "var konsekvent med det svaret" in system_message.content
    assert "motsäg aldrig ett tidigare svar i tysthet" in system_message.content


# --- B: understand the underlying goal, not just the literal suggestion ---


def test_work_command_carries_goal_understanding_instruction(client, superuser_db, _captured_chat_messages):
    csrf = _login(client)
    _send(client, csrf, "Skapa ett nytt dokument i biblioteket.")
    system_message = _system_message(_captured_chat_messages[-1])
    assert "BAKOMLIGGANDE MÅLET" in system_message.content
    assert "beslutet om vad som faktiskt görs är alltid grundarens" in system_message.content


def test_idea_marker_carries_goal_understanding_instruction(client, superuser_db, _captured_chat_messages):
    csrf = _login(client)
    _send(client, csrf, "Tänk om vi kunde lägga till export till PDF?")
    system_message = _system_message(_captured_chat_messages[-1])
    assert "BAKOMLIGGANDE MÅLET" in system_message.content


def test_plain_question_does_not_carry_goal_understanding_instruction(client, superuser_db, _captured_chat_messages):
    """A plain question is not a suggestion -- the instruction would be irrelevant noise and
    must not fire just because *some* cognitive-reliability block always does."""
    csrf = _login(client)
    _send(client, csrf, "Vad är kvantdatorer?")
    system_message = _system_message(_captured_chat_messages[-1])
    assert "BAKOMLIGGANDE MÅLET" not in system_message.content


# --- C: a realistic long conversation exercising reference resolution, repeated-question
# avoidance and goal-understanding together, continuing past Workstream D's own scenario ---


def _seed_pairs(superuser_db, conversation_id, n, *, label_from=0):
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for i in range(label_from, label_from + n):
        u = MessageModel(
            conversation_id=conversation_id, role=MessageRole.user, status=MessageStatus.succeeded,
            content=f"Meddelande {i} från grundaren.", created_at=base + datetime.timedelta(seconds=i * 20),
        )
        superuser_db.add(u)
        superuser_db.commit()
        a = MessageModel(
            conversation_id=conversation_id, role=MessageRole.assistant, status=MessageStatus.succeeded,
            content=f"Svar {i} från MainAI.", in_reply_to_id=u.id, created_at=base + datetime.timedelta(seconds=i * 20 + 10),
        )
        superuser_db.add(a)
        superuser_db.commit()


def test_long_conversation_with_reference_resolution_and_goal_understanding(client, superuser_db, _captured_chat_messages):
    """One realistic long-running founder conversation (well past the 20-message history
    window), ending with a pronoun-only follow-up and then a concrete suggestion -- asserting
    both that the most recent real content is still what the model actually sees (reference
    resolution has something real to resolve against) and that the goal-understanding
    instruction correctly fires on the suggestion turn but not the pronoun turn before it."""
    founder_id = _founder_id(superuser_db)
    conv_id = _create_conversation(superuser_db, founder_id)
    _seed_pairs(superuser_db, conv_id, 12)  # 24 messages, already past the 20-message window
    csrf = _login(client)

    # A pronoun-heavy follow-up referring to the most recent exchange -- not a suggestion, so
    # the goal-understanding instruction must NOT fire here.
    body_ref = _send(client, csrf, "Förklara det igen.", conversation_id=conv_id)
    assert body_ref["assistant_status"] == "succeeded"
    outgoing_ref = _captured_chat_messages[-1]
    history_texts = " ".join(m.content for m in outgoing_ref if m.role != "system")
    assert "Svar 11 från MainAI." in history_texts  # most recent real content, not the oldest
    assert "Meddelande 0 från grundaren." not in history_texts  # oldest content correctly dropped
    system_ref = _system_message(outgoing_ref)
    assert "BAKOMLIGGANDE MÅLET" not in system_ref.content
    assert "var konsekvent med det svaret" in system_ref.content  # history exists -> self-consistency applies

    # A concrete suggestion in the same conversation -- goal-understanding must fire now.
    body_suggest = _send(client, csrf, "Bygg en ny sida för det här.", conversation_id=conv_id)
    assert body_suggest["assistant_status"] == "succeeded"
    system_suggest = _system_message(_captured_chat_messages[-1])
    assert "BAKOMLIGGANDE MÅLET" in system_suggest.content

    # No unauthorized decision: every reply went through the one already-authorized /api/chat
    # path; this test added no new authorization surface.
    assert len(_captured_chat_messages) == 2
