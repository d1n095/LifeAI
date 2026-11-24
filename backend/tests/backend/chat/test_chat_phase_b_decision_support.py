"""Phase B (Constructive Reasoning & Decision Support) — API-level tests against the real
/api/chat endpoint, real Postgres, a deterministic fake chat+embedding provider (same pattern
as test_chat_phase_a_conversational_intelligence.py).

Gap assessment (see this phase's dispatch report for the full version): the storage layer for
decision quality (app/problem_learning — LifeProblem/LifeProblemApproach/LifeProblemAssumption/
LifeProblemDecision) already exists, is already wired with a careful authority discipline
(authority="founder" vs "ai_interpretation", never auto-promoted — confirmed by grep: the ONLY
caller of record_decision() in the whole codebase outside this service is the corpus-trial
evaluation harness, never a live router), and already models alternatives/basis/supersession.
The real, narrow gap: `_decisions()` dropped `alternatives`/`basis` before they reached any
caller, and `build_project_status()`'s `decisions` and (newly added) `rejected_approaches` keys
were computed but never actually read by format_project_status_instructions() -- so a real
decision with real alternatives and a real rejected approach with its reason were both durable
but completely invisible to every live conversation. This file proves the fix: decisions and
rejected approaches now reach the real outgoing system prompt, with `authority` translated into
the IDEA/EXPLORATION/RECOMMENDATION/FOUNDER_DECISION/AUTHORIZED_IMPLEMENTATION vocabulary PR
#503's founder-vision register uses, and a new constructive-reasoning instruction block fires
on the same SUGGESTION_BEARING_INTENTS gate Phase A already built.

Same honesty standard as every file in this effort: these tests assert the correct instruction
text and real registered facts reach the outgoing prompt. They do NOT and cannot prove a live
model actually complies with the instruction.
"""

import uuid

import pytest

from app.config import get_settings
from app.problem_learning.service import create_problem, record_approach, record_decision, transition_approach
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


def _send(client, csrf, message):
    res = client.post("/api/chat", json={"message": message}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    return res.json()


def _system_message(captured_batch):
    return next(m for m in captured_batch if m.role == "system")


def _make_problem(superuser_db, owner_id, key_suffix):
    row = create_problem(
        superuser_db,
        owner_id=owner_id,
        title=f"Testproblem {key_suffix}",
        description="Phase B test fixture problem.",
        idempotency_key=f"phase-b-test-problem:{key_suffix}",
    )
    superuser_db.commit()
    return row


# --- Decisions: authority translation + alternatives surfaced ---


def test_founder_decision_renders_as_binding_with_alternatives(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    problem = _make_problem(superuser_db, founder_id, "founder-decision")
    record_decision(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        decision="Använd Postgres med pgvector för retrieval.",
        idempotency_key="phase-b-test:decision-founder",
        status="active",
        authority="founder",
        basis="manual",
        alternatives=["Elasticsearch", "ren vektordatabas (Qdrant)"],
    )
    superuser_db.commit()

    csrf = _login(client)
    _send(client, csrf, "Vad är projektets nuvarande status?")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "FOUNDER_DECISION (bindande)" in system_message.content
    assert "Använd Postgres med pgvector för retrieval." in system_message.content
    assert "grund: manual" in system_message.content
    assert "Elasticsearch" in system_message.content
    assert "Qdrant" in system_message.content


def test_ai_recommendation_renders_as_not_yet_approved(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    problem = _make_problem(superuser_db, founder_id, "ai-recommendation")
    record_decision(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        decision="Byt till en hybrid lexikal+vektor-sökning.",
        idempotency_key="phase-b-test:decision-ai",
        status="active",
        authority="ai_interpretation",
        basis="inferred",
    )
    superuser_db.commit()

    csrf = _login(client)
    _send(client, csrf, "Vad är projektets nuvarande status?")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "RECOMMENDATION (inte ännu godkänd av grundaren)" in system_message.content
    assert "Byt till en hybrid lexikal+vektor-sökning." in system_message.content


def test_superseded_decision_no_longer_renders_as_current(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    problem = _make_problem(superuser_db, founder_id, "supersession")
    original = record_decision(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        decision="Kör allt på en enda VPS med 2 GB RAM.",
        idempotency_key="phase-b-test:decision-original",
        status="active",
        authority="founder",
    )
    superuser_db.commit()

    record_decision(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        decision="Flytta till en dedikerad VPS XL med 16 GB RAM.",
        idempotency_key="phase-b-test:decision-superseding",
        status="active",
        authority="founder",
        supersedes_decision_id=original.id,
    )
    superuser_db.commit()

    csrf = _login(client)
    _send(client, csrf, "Vad är projektets nuvarande status?")
    system_message = _system_message(_captured_chat_messages[-1])

    # New evidence correctly supersedes the old recommendation -- the current decision shows...
    assert "Flytta till en dedikerad VPS XL med 16 GB RAM." in system_message.content
    # ...and the superseded one no longer appears as a CURRENT fact (it remains queryable in
    # the database by design -- app/problem_learning never deletes -- just not re-surfaced here).
    assert "Kör allt på en enda VPS med 2 GB RAM." not in system_message.content


# --- Rejected approaches: "inspect existing solutions" / don't re-propose what failed ---


def test_rejected_approach_surfaces_with_its_reason(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    problem = _make_problem(superuser_db, founder_id, "rejected-approach")
    approach = record_approach(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        description="Bygg en egen vektorsökmotor från grunden.",
        idempotency_key="phase-b-test:approach-rejected",
    )
    superuser_db.commit()
    transition_approach(
        superuser_db,
        owner_id=founder_id,
        approach_id=approach.id,
        status="rejected",
        reason="Orimlig underhållskostnad jämfört med pgvector.",
    )
    superuser_db.commit()

    csrf = _login(client)
    _send(client, csrf, "Vad är projektets nuvarande status?")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "Bygg en egen vektorsökmotor från grunden." in system_message.content
    assert "Orimlig underhållskostnad jämfört med pgvector." in system_message.content
    assert "föreslå inte samma sak igen" in system_message.content.lower()


def test_non_rejected_approach_does_not_appear_in_rejected_section(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    problem = _make_problem(superuser_db, founder_id, "active-approach")
    record_approach(
        superuser_db,
        owner_id=founder_id,
        problem_id=problem.id,
        description="En helt vanlig, fortfarande aktiv lösning.",
        idempotency_key="phase-b-test:approach-active",
        status="proposed",
    )
    superuser_db.commit()

    csrf = _login(client)
    _send(client, csrf, "Vad är projektets nuvarande status?")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "En helt vanlig, fortfarande aktiv lösning." not in system_message.content


# --- Constructive reasoning instruction: same SUGGESTION_BEARING_INTENTS gate as Phase A ---


def test_suggestion_carries_constructive_reasoning_instruction(client, superuser_db, _captured_chat_messages):
    csrf = _login(client)
    _send(client, csrf, "Vi borde bygga en egen vektorsökmotor från grunden.")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "identifiera vilka antaganden" in system_message.content
    assert "EXPLORATION/RECOMMENDATION" in system_message.content
    assert "aldrig ett FOUNDER_DECISION" in system_message.content


def test_plain_question_does_not_carry_constructive_reasoning_instruction(client, superuser_db, _captured_chat_messages):
    csrf = _login(client)
    _send(client, csrf, "Vad är klockan ungefär i Sverige nu?")
    system_message = _system_message(_captured_chat_messages[-1])

    assert "identifiera vilka antaganden" not in system_message.content
