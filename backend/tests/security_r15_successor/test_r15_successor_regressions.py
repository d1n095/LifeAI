"""Negative regression tests for the R15 successor security remediation.

Each test is written to FAIL against PR #464 @ 8667146 before the corresponding fix:

* R15-01: a known-price external model must be admissible for a reservation, while an
  unknown-price external model must still be refused.
* R15-04: the chat request contract must accept a bounded client idempotency key.
* R15-05: legacy ``chat_with_fallback`` callers must opt into ``effect_guard`` so an
  ambiguous provider failure can never silently fall through to another provider.

Run without PostgreSQL/Redis:

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_successor -q \
      --confcutdir=tests/security_r15_successor
"""

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.billing import reservation
from app.billing.reservation import BillingPriceUnverifiedError, ChatAttemptBudget
from app.providers.base import ChatResult
from app.schemas import ChatMessageIn


def _budget(monkeypatch):
    """Build a real ChatAttemptBudget with only the external I/O faked."""

    import app.audit as audit

    monkeypatch.setattr(reservation, "reserve_compute", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        reservation,
        "estimate_reservation_cost",
        lambda *a, **k: Decimal("0.001"),
    )
    monkeypatch.setattr(audit, "record_audit", lambda *a, **k: None)

    db = SimpleNamespace(commit=lambda: None)
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=None)
    reply = SimpleNamespace(id=uuid.uuid4(), provider=None, model=None)
    budget = ChatAttemptBudget(
        db,
        user=user,
        limits=SimpleNamespace(),
        estimated_tokens=100,
        source_ref="chat:test:inflight:nonce",
        reply_slot=reply,
        enforce=True,
    )
    return budget, reply


def test_known_price_external_model_is_admissible_for_reservation(monkeypatch):
    budget, reply = _budget(monkeypatch)

    # gpt-4o-mini has a declared input price (0.15 / 1M). It must not be refused merely
    # because the inherited catalog snapshot has no independently captured as_of date.
    budget.before("openai", "gpt-4o-mini")

    assert budget.reservation is not None
    assert (reply.provider, reply.model) == ("openai", "gpt-4o-mini")


def test_unknown_price_external_model_is_still_refused(monkeypatch):
    budget, _ = _budget(monkeypatch)

    with pytest.raises(BillingPriceUnverifiedError):
        budget.before("zai", "glm-5.3")


def test_chat_request_accepts_bounded_client_idempotency_key():
    payload = ChatMessageIn(message="hej", client_message_id="retry-2026-11-20-1")
    assert payload.client_message_id == "retry-2026-11-20-1"


@pytest.mark.parametrize("bad", ["", "   ", "bad key\n", "x" * 129])
def test_chat_request_rejects_invalid_client_idempotency_key(bad):
    with pytest.raises(ValidationError):
        ChatMessageIn(message="hej", client_message_id=bad)


def test_planner_passes_effect_guard(monkeypatch):
    from app.mainai_execution import planner

    captured = []

    async def fake_chat(db, messages, **kwargs):
        captured.append(kwargs)
        return ChatResult("not json", "ollama", "gemma3:4b"), ["ollama"]

    monkeypatch.setattr(planner, "chat_with_fallback", fake_chat)
    goal = SimpleNamespace(
        title="test",
        original_instruction="test",
        owner_id=uuid.uuid4(),
        id=uuid.uuid4(),
    )

    with pytest.raises(planner.PlanValidationError):
        asyncio.run(planner.propose_plan_via_ai(None, goal=goal))

    assert captured
    assert all(call.get("effect_guard") is True for call in captured)


def test_read_only_audit_passes_effect_guard(monkeypatch):
    from app.mainai_execution import execution_job

    captured = {}

    async def fake_chat(db, messages, **kwargs):
        captured.update(kwargs)
        return ChatResult("summary", "ollama", "gemma3:4b"), ["ollama"]

    monkeypatch.setattr(execution_job, "chat_with_fallback", fake_chat)
    task = SimpleNamespace(
        owner_id=uuid.uuid4(),
        id=uuid.uuid4(),
        goal_id=uuid.uuid4(),
        description="review this",
    )

    result = asyncio.run(execution_job._handle_read_only_audit(None, task))

    assert result["summary"] == "summary"
    assert captured.get("effect_guard") is True


def test_agent_dispatch_passes_effect_guard(monkeypatch):
    from app import agent_orchestration

    captured = {}

    async def fake_chat(db, messages, **kwargs):
        captured.update(kwargs)
        return ChatResult("ok", "ollama", "gemma3:4b"), ["ollama"]

    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="task",
        description="do it",
        target_files=[],
        constraints=None,
        acceptance_criteria="works",
        required_tests=None,
    )
    monkeypatch.setattr(agent_orchestration, "get_task", lambda db, task_id: task)
    monkeypatch.setattr(agent_orchestration, "chat_with_fallback", fake_chat)
    monkeypatch.setattr(agent_orchestration, "_record_event", lambda *a, **k: "event")

    assert asyncio.run(
        agent_orchestration.dispatch_task(
            None, task.id, authorized_by_user_id=uuid.uuid4(), dispatched_by="test"
        )
    ) == "event"
    assert captured.get("effect_guard") is True
    assert captured.get("owner_id") is not None


def test_lesson_conflict_passes_effect_guard(monkeypatch):
    from app.mainai_execution import lesson_conflicts

    captured = {}

    async def fake_chat(db, messages, **kwargs):
        captured.update(kwargs)
        return ChatResult('{"conflict": false, "reasoning": "ok"}', "ollama", "gemma3:4b"), ["ollama"]

    monkeypatch.setattr(lesson_conflicts, "chat_with_fallback", fake_chat)
    lesson = SimpleNamespace(
        id=uuid.uuid4(),
        affected_component="component",
        general_rule="rule",
        fix="fix",
    )

    conflict, reasoning = asyncio.run(
        lesson_conflicts.detect_conflict(
            None, lesson_a=lesson, lesson_b=lesson, owner_id=uuid.uuid4()
        )
    )

    assert (conflict, reasoning) == (False, "ok")
    assert captured.get("effect_guard") is True
    assert captured.get("owner_id") is not None
