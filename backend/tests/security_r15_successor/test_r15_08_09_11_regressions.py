"""Negative regression tests for R15-08, R15-09 and R15-11.

DB-free, same isolated environment as the rest of ``tests/security_r15_successor``:

* R15-08: input and output tokens are priced at their own rates, the prompt estimate is
  character-based instead of UTF-8-byte-based, and a mixed hold is no longer priced as if
  every token were output.
* R15-09: an evaluation scope cannot nest, cannot use wildcard model identities, cannot be
  entered without authorization, and cannot fall back to a production provider.
* R15-11: concurrent identical reservations charge once, tenants stay isolated, and an
  ambiguous timeout keeps its hold while a proven pre-egress timeout releases it.

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_successor -q \
      --confcutdir=tests/security_r15_successor
"""

import contextvars
import threading
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.billing import chat_attempts, reservation
from app.billing.enforcement import ComputeBudgetStore
from app.billing.reservation import (
    ChatAttemptBudget,
    estimate_prompt_tokens,
    estimate_reservation_cost,
    estimate_text_tokens,
    estimate_usage_cost,
)
from app.providers import registry
from app.providers.base import ChatResult, LLMProvider, ProviderError
from app.providers.catalog import ModelSelectionError, ModelSelectionPolicy


# --- R15-08: split pricing and token estimation -----------------------------------------


def test_prompt_and_output_are_priced_at_their_own_rates():
    prompt_only = estimate_usage_cost(
        "openai", "gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=0
    )
    output_only = estimate_usage_cost(
        "openai", "gpt-4o-mini", prompt_tokens=0, completion_tokens=1_000_000
    )
    mixed = estimate_usage_cost(
        "openai", "gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )

    assert prompt_only == Decimal("0.15")
    assert output_only == Decimal("0.60")
    assert mixed == Decimal("0.75")


def test_total_only_reservation_stays_conservative_but_split_is_cheaper():
    legacy = estimate_reservation_cost(
        None, 1_000_000, provider="openai", model="gpt-4o-mini"
    )
    split = estimate_reservation_cost(
        None,
        1_000_000,
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )

    assert legacy == Decimal("0.60")
    assert split == Decimal("0.15")


def test_unknown_external_price_uses_the_configured_fallback_not_zero():
    settings = SimpleNamespace(billing_unknown_cost_fallback_usd_per_1k=0.01)

    cost = estimate_usage_cost(
        "zai",
        "glm-5.3",
        prompt_tokens=500,
        completion_tokens=500,
        settings=settings,
    )

    assert cost == Decimal("0.01")


def test_token_estimator_is_character_based_and_counts_non_ascii():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a" * 400) == 100
    assert estimate_text_tokens("å" * 100) == 100
    assert estimate_prompt_tokens(["a" * 400, ""]) == 108
    # The old byte-count + 1024 estimate was ~4x too large for ASCII prompts.
    assert estimate_prompt_tokens(["a" * 400]) < 400 + 1024


def _capture_budget(monkeypatch, **overrides):
    import app.audit as audit

    captured = {}
    monkeypatch.setattr(audit, "record_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        reservation,
        "reserve_compute",
        lambda _db, **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    budget = ChatAttemptBudget(
        db,
        user=SimpleNamespace(id=uuid.uuid4(), tenant_id=None),
        limits=SimpleNamespace(),
        estimated_tokens=overrides.pop("estimated_tokens", 100),
        source_ref="chat:test:r15-08",
        reply_slot=SimpleNamespace(id=uuid.uuid4(), provider=None, model=None),
        enforce=True,
        **overrides,
    )
    return budget, captured


def test_reservation_hold_uses_the_split_estimate(monkeypatch):
    budget, captured = _capture_budget(
        monkeypatch,
        estimated_tokens=1_500_000,
        estimated_prompt_tokens=1_000_000,
        estimated_completion_tokens=500_000,
    )

    budget.before("openai", "gpt-4o-mini")

    assert captured["estimated_tokens"] == 1_500_000
    assert captured["estimated_cost_usd"] == Decimal("0.45")


def test_settlement_uses_split_usage(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        reservation,
        "settle_reservation",
        lambda _reservations, **kwargs: captured.update(kwargs),
    )
    budget = ChatAttemptBudget(
        SimpleNamespace(commit=lambda: None),
        user=SimpleNamespace(id=uuid.uuid4(), tenant_id=None),
        limits=SimpleNamespace(),
        estimated_tokens=100,
        source_ref="chat:test:r15-08-settle",
        reply_slot=SimpleNamespace(id=uuid.uuid4(), provider=None, model=None),
        enforce=True,
    )
    budget.reservation = object()

    budget.succeeded(
        SimpleNamespace(
            raw_usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
            provider="openai",
            model="gpt-4o-mini",
        )
    )

    assert captured["actual_tokens"] == 1_000_000
    assert captured["actual_cost_usd"] == Decimal("0.15")


# --- R15-11: ambiguous effects, concurrency and tenant isolation -----------------------


def _failure_budget(monkeypatch):
    events = []
    monkeypatch.setattr(
        reservation, "release_reservation", lambda *_a, **_k: events.append(("release",))
    )
    for name in ("mark_held", "mark_released", "mark_release_pending"):
        monkeypatch.setattr(
            chat_attempts,
            name,
            lambda _db, _attempt, _name=name, **kwargs: events.append((_name, kwargs)),
        )
    budget = ChatAttemptBudget(
        SimpleNamespace(commit=lambda: None),
        user=SimpleNamespace(id=uuid.uuid4(), tenant_id=None),
        limits=SimpleNamespace(),
        estimated_tokens=100,
        source_ref="chat:test:r15-11",
        reply_slot=SimpleNamespace(id=uuid.uuid4(), provider=None, model=None),
        enforce=True,
    )
    budget.reservation = object()
    budget.attempt = SimpleNamespace(id=uuid.uuid4())
    return budget, events


def test_ambiguous_timeout_retains_the_hold(monkeypatch):
    budget, events = _failure_budget(monkeypatch)

    budget.failed(ProviderError("timeout", provider_request_may_have_left=True))

    assert ("release",) not in events
    held = [event for event in events if event[0] == "mark_held"]
    assert held and held[0][1]["evidence"]["reason"] == "ambiguous_external_effect"
    assert budget.reservation is not None


def test_proven_pre_egress_timeout_releases_the_hold(monkeypatch):
    budget, events = _failure_budget(monkeypatch)

    budget.failed(ProviderError("timed out before send", provider_request_may_have_left=False))

    assert ("release",) in events
    released = [event for event in events if event[0] == "mark_released"]
    assert released and released[0][1]["evidence"]["reason"] == "proven_pre_egress"
    assert budget.reservation is None


def test_parallel_identical_reservations_charge_once():
    store = ComputeBudgetStore(redis_url=None)
    subject = str(uuid.uuid4())
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            results.append(
                store.reserve(
                    subject_type="user",
                    subject_id=subject,
                    estimated_tokens=100,
                    estimated_cost_usd=Decimal("0.01"),
                    idempotency_key="same-request",
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len({item.reservation_id for item in results}) == 1
    remaining = store.remaining(
        subject_type="user",
        subject_id=subject,
        token_caps=(1000, 1000),
        cost_caps_micros=(1_000_000, 1_000_000),
    )
    assert remaining["day"] == (900, 990_000)
    assert remaining["month"] == (900, 990_000)


def test_tenant_reservations_are_isolated():
    store = ComputeBudgetStore(redis_url=None)
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    store.reserve(
        subject_type="tenant",
        subject_id=tenant_a,
        estimated_tokens=400,
        estimated_cost_usd=Decimal("0.01"),
    )
    store.reserve(
        subject_type="tenant",
        subject_id=tenant_b,
        estimated_tokens=100,
        estimated_cost_usd=Decimal("0.01"),
    )

    a = store.remaining(
        subject_type="tenant",
        subject_id=tenant_a,
        token_caps=(1000, 1000),
        cost_caps_micros=(1_000_000, 1_000_000),
    )
    b = store.remaining(
        subject_type="tenant",
        subject_id=tenant_b,
        token_caps=(1000, 1000),
        cost_caps_micros=(1_000_000, 1_000_000),
    )
    assert a["day"][0] == 600
    assert b["day"][0] == 900


# --- R15-09: evaluation isolation ------------------------------------------------------


class _FakeEvaluationProvider(LLMProvider):
    name = "zai_eval"

    def is_configured(self) -> bool:
        return True

    async def chat(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="eval", provider=self.name, model=model, raw_usage={})

    async def embed(self, texts, model, *, timeout=None):
        return [[0.0] for _ in texts]


class _NoProductionDb:
    def query(self, *_args, **_kwargs):
        raise AssertionError("evaluation must not consult the production provider config")


def _authorized(monkeypatch):
    monkeypatch.setattr(
        registry, "get_settings", lambda: SimpleNamespace(environment="test")
    )


def test_evaluation_requires_authorization_and_exact_models(monkeypatch):
    _authorized(monkeypatch)

    with pytest.raises(ProviderError, match="not authorized"):
        with registry.evaluation_provider_scope(
            _FakeEvaluationProvider,
            {"chat": "glm-eval", "embedding": "bow-eval"},
            authorized=False,
        ):
            pass

    with pytest.raises(ProviderError, match="exact"):
        with registry.evaluation_provider_scope(
            _FakeEvaluationProvider,
            {"chat": "glm-*", "embedding": "bow-eval"},
            authorized=True,
        ):
            pass


def test_evaluation_scope_cannot_nest(monkeypatch):
    _authorized(monkeypatch)
    models = {"chat": "glm-eval", "embedding": "bow-eval"}

    with registry.evaluation_provider_scope(
        _FakeEvaluationProvider, models, authorized=True
    ):
        with pytest.raises(ProviderError, match="already active"):
            with registry.evaluation_provider_scope(
                _FakeEvaluationProvider, models, authorized=True
            ):
                pass


def test_evaluation_context_cannot_outlive_its_scope(monkeypatch):
    _authorized(monkeypatch)

    with registry.evaluation_provider_scope(
        _FakeEvaluationProvider,
        {"chat": "glm-eval", "embedding": "bow-eval"},
        authorized=True,
    ):
        copied = contextvars.copy_context()

    with pytest.raises(ProviderError, match="expired"):
        copied.run(registry._current_evaluation)


def test_evaluation_never_falls_back_to_a_production_provider(monkeypatch):
    _authorized(monkeypatch)

    with pytest.raises(ProviderError):
        registry.get_provider("zai_eval")

    with registry.evaluation_provider_scope(
        _FakeEvaluationProvider,
        {"chat": "glm-eval", "embedding": "bow-eval"},
        authorized=True,
    ):
        provider, model = registry.resolve_active(_NoProductionDb(), "chat")
        assert isinstance(provider, _FakeEvaluationProvider)
        assert model == "glm-eval"

        chain = registry.resolve_chat_chain(_NoProductionDb(), selection_policy=None)
        assert [(item.name, item_model) for item, item_model in chain] == [
            ("zai_eval", "glm-eval")
        ]

        with pytest.raises(ModelSelectionError, match="evaluation"):
            registry.resolve_chat_chain(
                _NoProductionDb(), ModelSelectionPolicy(role="chat")
            )
