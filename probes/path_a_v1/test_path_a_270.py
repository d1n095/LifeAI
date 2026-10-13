"""Examiner probes added for #270 (d5153eb, SPEND-8/9/10 remediation). Real billed_execution_chat and
real chat_with_fallback; provider HTTP is never reached (provider.chat or the provider's low-level
_post is monkeypatched). Production spend gate forced on. MATRIX lines; never raise on GAP."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.config import get_settings
from app.mainai_execution import billed_chat
from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.providers import registry as provider_registry
from app.providers.base import ChatResult, Message
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.pricing import estimate_cost

from test_path_a_exam import M, _goal, _privileges, owner_id  # noqa: F401
from test_path_a_268 import _grant_custom, _real_chain


@pytest.fixture(autouse=True)
def _production_spend_gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)


async def _loop(db, owner_id, goal, messages, n):
    outcomes = []
    for _ in range(n):
        try:
            await billed_execution_chat(db, messages, owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
            db.commit()
            outcomes.append("accepted")
        except ExecutionSpendError as exc:
            db.rollback()
            outcomes.append(str(exc)[:70])
    return outcomes


def _grant_row(db, goal):
    db.expire_all()
    return db.query(ProviderSpendAuthorization).filter(ProviderSpendAuthorization.goal_id == goal.id).one()


@pytest.mark.asyncio
async def test_spend8_cost_hold_vs_token_hold(db_session, owner_id, monkeypatch):
    """Founder sets max_cost_per_request_usd=0.001 and max_cost_usd=0.01. Each answer stays INSIDE the
    token holds (prompt 7,000/8,000; completion == max_tokens passed)."""
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.01", max_requests=100, per_request="0.001")
    seen = []

    def _answer(messages):
        return ChatResult(content="ok", provider="openai", model="gpt-4o-mini", raw_usage={"prompt_tokens": 7_000, "completion_tokens": 4_000})

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _answer}, seen)
    outcomes = await _loop(db_session, owner_id, goal, [Message(role="user", content="x" * 21_000)], 30)
    real = estimate_cost("openai", "gpt-4o-mini", 7_000, 4_000) * len(seen)
    row = _grant_row(db_session, goal)
    M("SPEND-8a real paid cost stays within the grant when the per-request cost hold is smaller than the token holds",
      real <= Decimal("0.01"),
      f"grant max_cost_usd=0.01, max_cost_per_request_usd=0.001; max_tokens passed={sorted({s['kwargs'] and 'max_tokens' in s['kwargs'] for s in seen})}; "
      f"paid requests={len(seen)}; accepted={outcomes.count('accepted')}; ledger spent=${row.spent_cost_usd}; real cost=${real} "
      f"({real / Decimal('0.01'):.2f}x the grant); first outcome={outcomes[0]!r}")


@pytest.mark.asyncio
async def test_spend8_unicode_prompt_estimate(db_session, owner_id, monkeypatch):
    """chars/3 is not an upper bound on tokens: 20,000 emoji = 80,000 UTF-8 bytes, >= 20,000 tokens
    (byte-level BPE needs at least one token per emoji). The fake reports the MINIMUM: 20,000."""
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.05", max_requests=100)
    seen = []

    def _answer(messages):
        return ChatResult(content="ok", provider="openai", model="gpt-4o-mini", raw_usage={"prompt_tokens": 20_000, "completion_tokens": 100})

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _answer}, seen)
    text = "\U0001F600" * 20_000
    outcomes = await _loop(db_session, owner_id, goal, [Message(role="user", content=text)], 3)
    rows = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    M("SPEND-8b prompt larger than the hold is refused before paying (non-ASCII prompt)", len(seen) == 0,
      f"chars={len(text)}, utf8 bytes={len(text.encode())}, estimate=ceil(chars/3)={(len(text) + 2) // 3} vs prompt hold "
      f"{rows[0].reserved_prompt_tokens if rows else 8000}; paid requests={len(seen)}; outcomes={[o[:50] for o in outcomes]}; "
      f"real prompt cost/call=${estimate_cost('openai', 'gpt-4o-mini', 20_000, 100)} vs counted/call=${rows[0].cost_usd if rows else None}")


@pytest.mark.asyncio
async def test_spend8_gemini_completion_cap(db_session, owner_id, monkeypatch):
    """Grant allowlists gemini/gemini-2.5-flash. Is the reserved completion cap sent to the provider?"""
    goal = _goal(db_session, owner_id)
    from test_path_a_268 import _grant_custom as gc  # noqa: F401
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.provider_spend import authorize_provider_spend
    import uuid

    proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
    _, env = authorize_execution_scope(db_session, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                       authorized_paths=["backend/app/x.py"], authorized_capabilities=["read_file"], authorized_risk="low",
                                       envelope_idempotency_key=f"e-{uuid.uuid4()}")
    authorize_provider_spend(db_session, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=env.id, authorized_by="founder",
                             max_cost_usd=Decimal("1.00"), max_requests=5, idempotency_key=f"s-{uuid.uuid4()}",
                             allowed_providers=["gemini"], allowed_models=["gemini-2.5-flash"])
    db_session.commit()
    provider = GeminiProvider()
    monkeypatch.setattr(GeminiProvider, "is_configured", lambda self: True)
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, "gemini-2.5-flash")])
    monkeypatch.setattr("app.egress_policy.enforce_egress_policy", lambda db, *, payload, **kw: payload, raising=True)
    sent = []

    async def _post(self, action, model, payload, timeout):
        sent.append(payload)
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 9_000, "thoughtsTokenCount": 6_000}}

    monkeypatch.setattr(GeminiProvider, "_post", _post)
    outcome = await _loop(db_session, owner_id, goal, [Message(role="user", content="x")], 1)
    cap = (sent[0].get("generationConfig") or {}).get("maxOutputTokens") if sent else None
    M("SPEND-8c the reserved completion cap reaches the provider for every priced provider (gemini)", cap is not None,
      f"gemini payload keys={sorted(sent[0]) if sent else None}; maxOutputTokens={cap}; outcome={outcome[0][:60]!r}; "
      f"usage normalization drops thoughtsTokenCount (billed as output)")


@pytest.mark.asyncio
async def test_spend10b_concurrent_billable_and_ordinary_chat(db_session, owner_id, monkeypatch):
    """A Path A call (grant allows gpt-4o-mini only) is in flight while an ordinary chat call runs."""
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="1.00", max_requests=5, per_request="0.10", models=("gpt-4o-mini",))
    provider = OpenAIProvider()
    full_chain = [(provider, "gpt-4o"), (provider, "gpt-4o-mini")]
    original = provider_registry.resolve_chat_chain
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: list(full_chain))
    monkeypatch.setattr(provider_registry, "resolve_chat_chain", lambda db: list(full_chain))
    patched_global = provider_registry.resolve_chat_chain
    monkeypatch.setattr("app.egress_policy.enforce_egress_policy", lambda db, *, payload, **kw: payload, raising=True)
    gate = asyncio.Event()
    calls = []

    async def _chat(self, messages, model, **kwargs):
        who = messages[0].content
        calls.append((who, model, kwargs.get("max_tokens")))
        if who == "pathA":
            await gate.wait()
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 5})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    async def path_a():
        await billed_execution_chat(db_session, [Message(role="user", content="pathA")], owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
        return "accepted"

    task = asyncio.create_task(path_a())
    await asyncio.sleep(0.2)
    ordinary, _ = await provider_registry.chat_with_fallback(db_session, [Message(role="user", content="ordinary")])
    gate.set()
    pa = await task
    unchanged = provider_registry.resolve_chat_chain is patched_global
    pa_calls = [c for c in calls if c[0] == "pathA"]
    or_calls = [c for c in calls if c[0] == "ordinary"]
    ok = unchanged and or_calls == [("ordinary", "gpt-4o", None)] and pa_calls and all(m == "gpt-4o-mini" and mt for _, m, mt in pa_calls)
    M("SPEND-10b concurrent Path A and ordinary chat: no shared provider state (chain/max_tokens) leaks either way", bool(ok),
      f"registry untouched={unchanged}; ordinary call -> {or_calls}; Path A call -> {pa_calls}; path A outcome={pa}")
    assert original is not None
