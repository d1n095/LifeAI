"""Examiner probes added for #268 (effafbe, SPEND-7 remediation). Same conventions as
test_path_a_exam.py: MATRIX lines, never raise on GAP, no live provider call (provider classes
are monkeypatched; the REAL chat_with_fallback and billed_chat chain constraint are exercised
unless a probe says otherwise)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import billed_chat
from app.mainai_execution.billed_chat import ExecutionSpendError, billed_execution_chat
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.provider_spend import authorize_provider_spend
from app.providers import registry as provider_registry
from app.providers.base import ChatResult, Message
from app.providers.openai_provider import OpenAIProvider
from app.providers.pricing import estimate_cost

from test_path_a_exam import M, _call, _counting_fallback, _goal, _grant_n, _privileges, owner_id  # noqa: F401


@pytest.fixture(autouse=True)
def _production_spend_gate(monkeypatch):
    # The target's test conftest disables the gate for unlisted modules; probe the production default.
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)


def _grant_custom(db, owner_id, goal, *, max_cost, max_requests, per_request=None, models=("gpt-4o-mini",)):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                            authorized_paths=["backend/app/x.py"], authorized_capabilities=["read_file", "patch_file"],
                                            authorized_risk="low", envelope_idempotency_key=f"env-{uuid.uuid4()}")
    authorize_provider_spend(db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
                             max_cost_usd=Decimal(max_cost), max_requests=max_requests,
                             max_cost_per_request_usd=None if per_request is None else Decimal(per_request),
                             idempotency_key=f"spend-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=list(models))
    db.commit()


def _real_chain(monkeypatch, models, behaviours, seen):
    """Real chat_with_fallback over OpenAIProvider entries; provider.chat is faked per model."""
    provider = OpenAIProvider()
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, m) for m in models])
    monkeypatch.setattr("app.egress_policy.enforce_egress_policy",
                        lambda db, *, payload, **kw: payload, raising=True)

    async def _chat(self, messages, model, **kwargs):
        seen.append({"model": model, "kwargs": sorted(kwargs), "chars": sum(len(m.content) for m in messages)})
        return behaviours[model](messages)

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)


@pytest.mark.asyncio
async def test_spend7b_settlement_failure_keeps_the_paid_request_counted(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_n(db_session, owner_id, goal, max_requests=2)
    calls = _counting_fallback(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("settle exploded")

    monkeypatch.setattr(billed_chat, "settle_provider_spend_call", _boom)
    outcomes = [await _call(db_session, owner_id, goal) for _ in range(4)]
    db_session.expire_all()
    rows = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    statuses = [str(getattr(r.status, "value", r.status)) for r in rows]
    M("SPEND-7b settlement errors (both settle attempts fail) leave each paid request counted; ceiling still stops calls",
      len(calls) <= 2 and len(rows) == len(calls) and all(s == "reserved" for s in statuses),
      f"max_requests=2; paid provider requests={len(calls)}; usage rows={statuses}; outcomes={[o[:45] for o in outcomes]}")


@pytest.mark.asyncio
async def test_spend7c_provider_exception_after_reservation_is_counted(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_n(db_session, owner_id, goal, max_requests=2)
    seen = []

    def _timeout(messages):
        raise TimeoutError("read timeout after request was sent")

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _timeout}, seen)
    outcomes = [await _call(db_session, owner_id, goal) for _ in range(3)]
    db_session.expire_all()
    rows = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    M("SPEND-7c provider exception after the request was sent is counted (no release) and survives job rollback",
      len(seen) == 2 and len(rows) == 2 and all(str(getattr(r.status, "value", r.status)) == "settled" for r in rows),
      f"provider invocations={len(seen)}; rows={[(str(getattr(r.status,'value',r.status)), str(r.cost_usd)) for r in rows]}; outcomes={[o[:45] for o in outcomes]}")


@pytest.mark.asyncio
async def test_spend8_hold_does_not_bound_the_paid_request(db_session, owner_id, monkeypatch):
    """Realistic code-agent prompt (~80k chars context => ~25k prompt tokens) vs the default 8k-token hold."""
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="0.05", max_requests=100)
    seen = []

    def _big(messages):
        return ChatResult(content="ok", provider="openai", model="gpt-4o-mini",
                          raw_usage={"prompt_tokens": 25_000, "completion_tokens": 2_048})

    _real_chain(monkeypatch, ["gpt-4o-mini"], {"gpt-4o-mini": _big}, seen)
    prompt = [Message(role="system", content="s"), Message(role="user", content="x" * 80_000)]
    outcomes = []
    for _ in range(40):
        try:
            await billed_execution_chat(db_session, prompt, owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
            db_session.commit()
            outcomes.append("accepted")
        except ExecutionSpendError as exc:
            db_session.rollback()
            outcomes.append(str(exc)[:60])
    db_session.expire_all()
    rows = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    counted = sum(Decimal(str(r.cost_usd or 0)) + (Decimal(str(r.reserved_cost_usd or 0)) if str(getattr(r.status, "value", r.status)) == "reserved" else 0) for r in rows)
    actual = estimate_cost("openai", "gpt-4o-mini", 25_000, 2_048) * len(seen)
    hold = rows[0].reserved_prompt_tokens if rows else None
    max_tokens_passed = any("max_tokens" in s["kwargs"] for s in seen)
    accepted = outcomes.count("accepted")
    M("SPEND-8 the reservation bounds the paid request (oversized prompt refused BEFORE paying; real cost <= grant max_cost_usd)",
      actual <= Decimal("0.05") and accepted == 0 and len(seen) == 0,
      f"grant max_cost_usd=0.05; prompt chars={seen[0]['chars'] if seen else 'n/a'} vs reserved_prompt_tokens={hold}; "
      f"max_tokens passed to provider={max_tokens_passed}; paid requests={len(seen)}; accepted={accepted}; "
      f"ledger counted=${counted}; real cost=${actual} ({(actual / counted if counted else 0):.2f}x); first outcome={outcomes[0]!r}")


@pytest.mark.asyncio
async def test_spend9_fallback_inside_one_reservation(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal, max_cost="1.00", max_requests=1, per_request="0.50", models=("gpt-4o-mini", "gpt-4o"))
    seen = []

    def _timeout(messages):
        raise TimeoutError("read timeout after request was sent")

    def _ok(messages):
        return ChatResult(content="ok", provider="openai", model="gpt-4o", raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    _real_chain(monkeypatch, ["gpt-4o-mini", "gpt-4o"], {"gpt-4o-mini": _timeout, "gpt-4o": _ok}, seen)
    outcome = await _call(db_session, owner_id, goal)
    db_session.expire_all()
    rows = db_session.query(ProviderSpendUsageEvent).filter(ProviderSpendUsageEvent.goal_id == goal.id).all()
    M("SPEND-9 max_requests bounds provider invocations when the authorized chain falls back",
      len(seen) <= 1,
      f"max_requests=1; provider invocations={[s['model'] for s in seen]}; usage rows={[(r.model, str(getattr(r.status,'value',r.status))) for r in rows]}; outcome={outcome[:40]}")


@pytest.mark.asyncio
async def test_spend10_chain_constraint_is_not_leaked_process_wide(db_session, owner_id, monkeypatch):
    """Two overlapping Path A calls (e.g. two plan requests in one API process)."""
    original = provider_registry.resolve_chat_chain
    goal_a, goal_b = _goal(db_session, owner_id), _goal(db_session, owner_id)
    _grant_custom(db_session, owner_id, goal_a, max_cost="1.00", max_requests=5, per_request="0.10", models=("gpt-4o-mini",))
    _grant_custom(db_session, owner_id, goal_b, max_cost="1.00", max_requests=5, per_request="0.10", models=("gpt-4o",))
    provider = OpenAIProvider()
    monkeypatch.setattr(billed_chat, "resolve_chat_chain", lambda db: [(provider, "gpt-4o-mini"), (provider, "gpt-4o")])
    monkeypatch.setattr("app.egress_policy.enforce_egress_policy", lambda db, *, payload, **kw: payload, raising=True)
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    seen = []

    async def _chat(self, messages, model, **kwargs):
        seen.append(model)
        await (gate_a if model == "gpt-4o-mini" else gate_b).wait()
        return ChatResult(content="ok", provider="openai", model=model, raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    from app.db import SessionLocal
    db_b = SessionLocal()
    from sqlalchemy import text as sa_text
    db_b.execute(sa_text("SET app.current_user_id = :uid"), {"uid": str(owner_id)})

    async def run(db, goal):
        try:
            await billed_execution_chat(db, [Message(role="user", content="x")], owner_id=owner_id, goal_id=goal.id, purpose="exam", requested_by="exam")
            return "accepted"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    ta = asyncio.create_task(run(db_session, goal_a))
    await asyncio.sleep(0.2)
    tb = asyncio.create_task(run(db_b, goal_b))
    await asyncio.sleep(0.2)
    gate_a.set()
    ra = await ta
    gate_b.set()
    rb = await tb
    leaked = provider_registry.resolve_chat_chain is not original
    leaked_chain = [m for _, m in provider_registry.resolve_chat_chain(None)] if leaked else None
    provider_registry.resolve_chat_chain = original  # restore for the rest of the run
    db_b.rollback()
    db_b.close()
    M("SPEND-10 per-call chain constraint does not leak into the process-wide provider registry",
      not leaked,
      f"outcomes A={ra} B={rb}; registry.resolve_chat_chain restored={not leaked}; leaked chain for ALL later chat callers={leaked_chain}")
