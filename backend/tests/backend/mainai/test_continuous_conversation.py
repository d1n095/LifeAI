"""Continuous founder↔MainAI conversation: founder talks, MainAI manages machines.

GitHub-discoverable facts are never asked of the founder. Busy agents are not
reassigned. Conversation state is not a security permission.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.mainai_continuous_conversation as pkg
from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.discover import branch_from_founder_text
from app.mainai_continuous_conversation.occupancy import founder_alpha_occupancy
from app.mainai_continuous_conversation.orchestrate import founder_alpha_continuous_turn, handle_founder_message
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.service import compose_founder_reply, get_or_create_canonical_conversation, persist_turn
from app.mainai_continuous_conversation.types import SoftwareTruth
from app.mainai_continuous_conversation.types import (
    InboundKind,
    InternalActionKind,
    InterruptKind,
    OutboundDisposition,
    RelayCategory,
)
from app.models.continuous_conversation import FounderCanonicalConversation, FounderConversationEvent
from app.models.conversation import Conversation
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.config import get_settings

FORBIDDEN_CALLS = frozenset(
    {
        "authorize_execution_scope",
        "authorize_provider_spend",
        "activate_kill_switch",
        "activate_global_kill_switch",
        "create_work_assignment",
        "authorize_work_candidate",
    }
)


def test_sha_question_is_internal_github_discovery_not_founder_relay():
    result = founder_alpha_continuous_turn("What SHA is the frozen Founder Alpha branch at?")
    assert result.inbound.kind is InboundKind.RELAY_REQUEST
    assert RelayCategory.SHA in result.inbound.relay_categories
    assert result.interrupt_founder is False
    assert result.founder_message is None
    assert any(action.kind is InternalActionKind.DISCOVER_FROM_GITHUB for action in result.internal_actions)


def test_busy_claude_is_not_reassigned_and_idle_agents_get_independent_lanes():
    result = founder_alpha_continuous_turn(
        "Tell Claude to start a second job and also assign Cursor and Codex something useful."
    )
    held = {action.agent_key for action in result.internal_actions if action.kind is InternalActionKind.HOLD_BUSY_AGENT}
    assigned = {action.agent_key for action in result.internal_actions if action.kind is InternalActionKind.ASSIGN_IDLE_AGENT}
    assert held == {"claude"}
    assert "cursor" in assigned
    assert "codex" in assigned
    assert result.interrupt_founder is False


def test_outbound_rewrite_blocks_asking_founder_for_sha():
    decision = filter_outbound("Please paste the exact SHA so I can give it to Codex.")
    assert decision.disposition is OutboundDisposition.REWRITE
    assert RelayCategory.SHA in decision.blocked_categories
    assert "paste" not in decision.content.lower() or "sha" not in decision.content.lower()
    assert decision.asks_founder_to_relay is True


def test_compose_founder_reply_uses_discovered_sha_instead_of_asking_founder():
    truth = SoftwareTruth(
        branch="codex/founder-alpha-final-composed-candidate",
        sha="691490edd82fa4bff6188f4f038f7579ee4f3df5",
        source="github",
    )
    reply = compose_founder_reply(
        draft="Please paste the exact SHA so I can give it to Codex.",
        discovered=truth,
    )
    assert "691490edd82fa4bff6188f4f038f7579ee4f3df5" in reply
    assert "paste" not in reply.lower()
    assert branch_from_founder_text("What SHA is the frozen Founder Alpha branch at?") == "codex/founder-alpha-final-composed-candidate"


def test_occupancy_fallback_holds_claude_and_keeps_cursor_idle():
    busy, idle = founder_alpha_occupancy()
    assert "claude" in busy
    assert "cursor" in idle
    assert "codex" in idle


def test_outbound_allows_ordinary_answer():
    decision = filter_outbound("I will read GitHub and keep coordinating Cursor on a separate lane.")
    assert decision.disposition is OutboundDisposition.SEND
    assert decision.content.startswith("I will read GitHub")


def test_budget_approval_interrupts_founder():
    inbound = classify_inbound("Approve $400 extra Claude spend for this week")
    assert inbound.kind is InboundKind.AUTHORITY_REQUEST
    assert inbound.interrupt is InterruptKind.MONEY_BUDGET
    assert inbound.interrupt_founder is True


def test_destructive_action_interrupts_founder():
    inbound = classify_inbound("Delete branch and drop database for the frozen candidate")
    assert inbound.interrupt is InterruptKind.DESTRUCTIVE_ACTION
    assert inbound.interrupt_founder is True


def test_status_question_does_not_interrupt():
    result = handle_founder_message("What is Claude doing?", busy_agents=("claude",))
    assert result.inbound.kind is InboundKind.STATUS_QUESTION
    assert result.interrupt_founder is False
    assert any(action.kind is InternalActionKind.HOLD_BUSY_AGENT for action in result.internal_actions)


def test_founder_alpha_draft_outbound_is_rewritten_not_sent():
    result = founder_alpha_continuous_turn(
        "Keep going.",
        draft_outbound="Please tell me which SHA to paste to Claude and who should work next.",
    )
    assert result.outbound is not None
    assert result.outbound.asks_founder_to_relay is True
    assert result.interrupt_founder is False


def _all_module_names() -> list[str]:
    return [f"app.mainai_continuous_conversation.{module.name}" for module in pkgutil.iter_modules(pkg.__path__)]


def test_package_does_not_call_forbidden_authority_functions():
    hits: dict[str, set[str]] = {}
    for name in _all_module_names():
        module = importlib.import_module(name)
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS
        }
        if called:
            hits[name] = called
    assert hits == {}


def _owner(db):
    user = User(email=f"cc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_canonical_conversation_is_stable_per_owner(superuser_db):
    owner = _owner(superuser_db)
    first = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    second = get_or_create_canonical_conversation(superuser_db, owner_id=owner.id)
    assert first.id == second.id
    bindings = superuser_db.query(FounderCanonicalConversation).filter_by(owner_id=owner.id).all()
    assert len(bindings) == 1
    assert superuser_db.get(Conversation, first.id).user_id == owner.id


def test_persist_turn_records_internal_actions_not_founder_interrupt(superuser_db):
    owner = _owner(superuser_db)
    result = persist_turn(
        superuser_db,
        owner_id=owner.id,
        text="What SHA is the frozen branch at? Tell Claude to start a second job.",
        busy_agents=("claude",),
        idle_agents=("cursor", "codex"),
        draft_outbound="Please paste the SHA to Claude.",
    )
    superuser_db.flush()
    assert result.interrupt_founder is False
    events = superuser_db.query(FounderConversationEvent).filter_by(owner_id=owner.id).all()
    directions = {event.direction for event in events}
    assert "inbound" in directions
    assert "internal" in directions
    assert "outbound" in directions
    assert any(event.kind == "discover_from_github" for event in events)
    assert any(event.kind == "hold_busy_agent" for event in events)
    assert any(event.suppressed for event in events if event.direction == "outbound")


def test_events_cannot_use_unknown_direction(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.add(
        FounderConversationEvent(
            owner_id=owner.id,
            direction="sidechannel",
            kind="oops",
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_rls_hides_other_owners_canonical_rows(db_session, superuser_db, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    get_or_create_canonical_conversation(superuser_db, owner_id=user_a.id)
    get_or_create_canonical_conversation(superuser_db, owner_id=user_b.id)
    superuser_db.commit()
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_a.id)})
    visible = db_session.query(FounderCanonicalConversation).all()
    assert [row.owner_id for row in visible] == [user_a.id]


FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim


def test_live_chat_continuous_thread_discovers_sha_internally_and_blocks_relay(client, superuser_db, monkeypatch):
    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        return ChatResult(
            content="Please paste the exact SHA so I can give it to Codex.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    async def _discover(text, *, client=None):
        return SoftwareTruth(
            branch="codex/founder-alpha-final-composed-candidate",
            sha="691490edd82fa4bff6188f4f038f7579ee4f3df5",
            source="github",
            detail="read from GitHub ref",
        )

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    monkeypatch.setattr("app.routers.chat.discover_software_truth", _discover)
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    first = client.post(
        "/api/chat",
        json={"message": "What SHA is the frozen Founder Alpha branch at?", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["assistant_status"] == "succeeded"
    assert "paste" not in body["reply"].lower()
    assert "691490edd82fa4bff6188f4f038f7579ee4f3df5" in body["reply"]
    assert "GitHub" in body["reply"]
    conversation_id = body["conversation_id"]
    second = client.post(
        "/api/chat",
        json={"message": "Keep going.", "continuous": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    events = superuser_db.query(FounderConversationEvent).filter_by(conversation_id=conversation_id).all()
    assert any(event.direction == "inbound" and event.kind == "relay_request" for event in events)
    assert any(event.kind == "discover_from_github" for event in events)
    assert any(event.kind == "hold_busy_agent" and (event.payload or {}).get("agent_key") == "claude" for event in events)
    assert any(event.kind == "assign_idle_agent" and (event.payload or {}).get("agent_key") == "cursor" for event in events)
