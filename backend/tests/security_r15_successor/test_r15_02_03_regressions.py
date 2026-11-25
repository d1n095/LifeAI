"""Negative regression tests for R15-02 (unresolved chat attempts) and R15-03 (grounding).

These are DB-free: they exercise the durable state-machine policy, the budget observer's
release/hold decisions, and the guarded retrieval boundary with only external I/O faked.

Run without PostgreSQL/Redis:

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_successor -q \
      --confcutdir=tests/security_r15_successor
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.billing import chat_attempts, reservation
from app.billing.chat_attempts import (
    DEFAULT_STALE_AFTER_SECONDS,
    RELEASE_PRE_EGRESS,
    SETTLE_CONSERVATIVE,
    SETTLE_FROM_USAGE,
    ReconciliationDenied,
    decide_reconciliation,
    reservations_from_record,
    serialize_reservations,
)
from app.billing.enforcement import BillingEnforcementUnavailableError, Reservation
from app.billing.reservation import ChatAttemptBudget
from app.providers.base import ProviderError


def _reservation() -> Reservation:
    return Reservation(
        subject_type="user",
        subject_id=str(uuid.uuid4()),
        tokens=120,
        cost_micros=5000,
        period_keys=("billing:day", "billing:month"),
        reservation_id="reservation-identity",
    )


def _budget(monkeypatch, *, state="held", boundary="ambiguous"):
    """A real ChatAttemptBudget with only DB/Redis interactions replaced."""

    events: list[tuple] = []

    def release(_reservations):
        events.append(("release",))

    def settle(*_args, **_kwargs):
        events.append(("settle",))

    monkeypatch.setattr(reservation, "release_reservation", release)
    monkeypatch.setattr(reservation, "settle_reservation", settle)
    for name in (
        "mark_held",
        "mark_released",
        "mark_release_pending",
        "mark_settled",
        "mark_settlement_pending",
    ):
        monkeypatch.setattr(
            chat_attempts,
            name,
            lambda db, attempt, _name=name, **kwargs: events.append((_name, kwargs)),
        )

    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    reply = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        in_reply_to_id=uuid.uuid4(),
        provider=None,
        model=None,
    )
    budget = ChatAttemptBudget(
        db,
        user=SimpleNamespace(id=uuid.uuid4(), tenant_id=None),
        limits=SimpleNamespace(),
        estimated_tokens=100,
        source_ref="chat:test:inflight:nonce",
        reply_slot=reply,
        enforce=True,
    )
    budget.reservation = object()
    budget.attempt = SimpleNamespace(
        id=uuid.uuid4(), state=state, effect_boundary=boundary, evidence={}
    )
    return budget, events


# --- durable reservation identity -------------------------------------------------------


def test_reservation_identity_round_trips_for_restart_recovery():
    original = _reservation()
    record = serialize_reservations(SimpleNamespace(_each=lambda: iter([original])))
    restored = reservations_from_record(record)

    assert restored == [original]
    assert restored[0].period_keys == ("billing:day", "billing:month")


# --- release is evidence-gated ----------------------------------------------------------


def test_ambiguous_attempt_cannot_be_released():
    denial = decide_reconciliation(
        state="held",
        effect_boundary="ambiguous",
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="incident-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    )

    assert denial is not None
    assert "kan ha nått leverantören" in denial


def test_invoking_attempt_cannot_be_released():
    denial = decide_reconciliation(
        state="invoking",
        effect_boundary="ambiguous",
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="incident-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    )

    assert denial is not None


def test_stale_never_invoked_attempt_may_be_released_with_evidence():
    assert decide_reconciliation(
        state="reserved",
        effect_boundary=None,
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="incident-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    ) is None


def test_live_attempt_cannot_be_released_even_if_never_invoked():
    denial = decide_reconciliation(
        state="reserved",
        effect_boundary=None,
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="incident-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS - 1,
        has_usage_evidence=False,
    )

    assert denial is not None
    assert "inte tillräckligt gammalt" in denial


def test_release_requires_a_reviewable_evidence_reference():
    denial = decide_reconciliation(
        state="reserved",
        effect_boundary=None,
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="short",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    )

    assert denial is not None
    assert "bevisreferens" in denial


# --- conservative settlement is the only ambiguous-resolution path ----------------------


def test_ambiguous_attempt_may_be_settled_conservatively():
    assert decide_reconciliation(
        state="held",
        effect_boundary="ambiguous",
        resolution=SETTLE_CONSERVATIVE,
        evidence_ref="provider-receipt-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    ) is None


def test_never_invoked_attempt_must_not_be_charged_conservatively():
    denial = decide_reconciliation(
        state="reserved",
        effect_boundary=None,
        resolution=SETTLE_CONSERVATIVE,
        evidence_ref="provider-receipt-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    )

    assert denial is not None
    assert "oanropat" in denial


def test_settle_from_usage_requires_recorded_usage():
    denial = decide_reconciliation(
        state="held",
        effect_boundary="ambiguous",
        resolution=SETTLE_FROM_USAGE,
        evidence_ref="usage-export-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=False,
    )

    assert denial is not None
    assert "dokumenterad" in denial

    assert decide_reconciliation(
        state="held",
        effect_boundary="ambiguous",
        resolution=SETTLE_FROM_USAGE,
        evidence_ref="usage-export-2026-11-20",
        age_seconds=DEFAULT_STALE_AFTER_SECONDS + 1,
        has_usage_evidence=True,
    ) is None


# --- budget observer never releases an unknown effect -----------------------------------


def test_ambiguous_provider_failure_retains_hold_and_records_it(monkeypatch):
    budget, events = _budget(monkeypatch)

    budget.failed(ProviderError("timeout", provider_request_may_have_left=None))

    assert ("release",) not in events
    held = [event for event in events if event[0] == "mark_held"]
    assert held and held[0][1]["evidence"]["reason"] == "ambiguous_external_effect"
    assert budget.reservation is not None


def test_proven_pre_egress_failure_releases_and_records_release(monkeypatch):
    budget, events = _budget(monkeypatch)

    budget.failed(
        ProviderError("not sent", provider_request_may_have_left=False)
    )

    assert ("release",) in events
    released = [event for event in events if event[0] == "mark_released"]
    assert released and released[0][1]["evidence"]["reason"] == "proven_pre_egress"
    assert budget.reservation is None


def test_settlement_store_failure_does_not_lose_a_successful_reply(monkeypatch):
    budget, events = _budget(monkeypatch)

    def broken_settle(*_args, **_kwargs):
        raise BillingEnforcementUnavailableError("redis down")

    monkeypatch.setattr(reservation, "settle_reservation", broken_settle)

    budget.succeeded(
        SimpleNamespace(
            raw_usage={"prompt_tokens": 10, "completion_tokens": 5},
            provider="ollama",
            model="gemma3:4b",
        )
    )

    pending = [event for event in events if event[0] == "mark_settlement_pending"]
    assert pending
    assert pending[0][1]["evidence"]["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }


def test_reconciliation_service_refuses_ambiguous_release_without_writes(monkeypatch):
    """The write path and the pure policy must agree: a held attempt is never released."""

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return SimpleNamespace(
                id=uuid.uuid4(),
                owner_id=uuid.uuid4(),
                state="held",
                effect_boundary="ambiguous",
                evidence={},
                updated_at=None,
                created_at=None,
                reservations=serialize_reservations(SimpleNamespace(_each=lambda: iter([_reservation()]))),
            )

    class FakeDb:
        def query(self, *_args, **_kwargs):
            return FakeQuery()

        def rollback(self):
            raise AssertionError("rollback should not be needed for a policy denial")

    with pytest.raises(ReconciliationDenied):
        chat_attempts.reconcile_attempt(
            FakeDb(),
            owner_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            resolution=RELEASE_PRE_EGRESS,
            evidence_ref="incident-2026-11-20",
            actor_id=uuid.uuid4(),
        )


# --- durable identity is written before invocation, and never lost on a write failure ---


def test_before_persists_attempt_identity_before_the_provider_marker(monkeypatch):
    import app.audit as audit

    budget, _events = _budget(monkeypatch)
    budget.reservation = None
    captured = {}

    monkeypatch.setattr(audit, "record_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        reservation,
        "reserve_compute",
        lambda *_a, **_k: SimpleNamespace(_each=lambda: iter([_reservation()])),
    )

    def create_attempt(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(chat_attempts, "create_attempt", create_attempt)

    budget.before("ollama", "gemma3:4b")

    assert captured["owner_id"] == budget.user.id
    assert captured["conversation_id"] == budget.reply_slot.conversation_id
    assert captured["user_message_id"] == budget.reply_slot.in_reply_to_id
    assert captured["assistant_message_id"] == budget.reply_slot.id
    assert captured["source_ref"] == budget.source_ref
    assert budget.attempt is not None


def test_attempt_write_failure_releases_the_hold_and_raises(monkeypatch):
    import app.audit as audit

    budget, events = _budget(monkeypatch)
    budget.reservation = None

    monkeypatch.setattr(audit, "record_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        reservation,
        "reserve_compute",
        lambda *_a, **_k: SimpleNamespace(_each=lambda: iter([_reservation()])),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("attempt row unavailable")

    monkeypatch.setattr(chat_attempts, "create_attempt", fail)

    with pytest.raises(RuntimeError, match="attempt row unavailable"):
        budget.before("ollama", "gemma3:4b")

    assert ("release",) in events
    assert budget.reservation is None


# --- reconciliation write path: finalize first, then persist the terminal state ----------


def _fake_reconcile_db(attempt):
    class Query:
        def filter(self, *_args, **_kwargs):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return attempt

    class Db:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def query(self, *_args, **_kwargs):
            return Query()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def refresh(self, _obj):
            return None

        def flush(self):
            return None

    return Db()


def _fake_store(events):
    class Store:
        def release(self, reservation):
            events.append(("release", reservation.reservation_id))

        def settle(self, reservation, *, actual_tokens, actual_cost_usd):
            events.append(
                ("settle", reservation.reservation_id, actual_tokens, actual_cost_usd)
            )

    return Store()


def test_reconcile_release_finalizes_store_and_marks_pre_egress(monkeypatch):
    owner = uuid.uuid4()
    message = SimpleNamespace(status="failed", error_category=None, content="old")
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=owner,
        conversation_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
        state="reserved",
        effect_boundary=None,
        evidence={},
        reservations=serialize_reservations(
            SimpleNamespace(_each=lambda: iter([_reservation()]))
        ),
        provider="ollama",
        model="gemma3:4b",
        created_at=datetime.utcnow() - timedelta(seconds=901),
        updated_at=datetime.utcnow() - timedelta(seconds=901),
    )
    db = _fake_reconcile_db(attempt)
    events = []

    monkeypatch.setattr(chat_attempts, "_store", lambda: _fake_store(events))
    monkeypatch.setattr(chat_attempts, "_message", lambda _db, _attempt: message)
    monkeypatch.setattr(chat_attempts, "record_audit", lambda *_a, **_k: None)

    result = chat_attempts.reconcile_attempt(
        db,
        owner_id=owner,
        attempt_id=attempt.id,
        resolution=RELEASE_PRE_EGRESS,
        evidence_ref="incident-2026-11-20",
        actor_id=owner,
    )

    assert events == [("release", "reservation-identity")]
    assert result.state == "released"
    assert message.error_category == "provider_pre_egress_released"
    assert message.content == ""
    assert db.commits == 1 and db.rollbacks == 0


def test_reconcile_conservative_settles_reserved_amount_without_refund(monkeypatch):
    owner = uuid.uuid4()
    message = SimpleNamespace(status="failed", error_category=None, content="old")
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=owner,
        conversation_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
        state="held",
        effect_boundary="ambiguous",
        evidence={},
        reservations=serialize_reservations(
            SimpleNamespace(_each=lambda: iter([_reservation()]))
        ),
        provider="ollama",
        model="gemma3:4b",
        created_at=datetime.utcnow() - timedelta(seconds=901),
        updated_at=datetime.utcnow() - timedelta(seconds=901),
    )
    db = _fake_reconcile_db(attempt)
    events = []

    monkeypatch.setattr(chat_attempts, "_store", lambda: _fake_store(events))
    monkeypatch.setattr(chat_attempts, "_message", lambda _db, _attempt: message)
    monkeypatch.setattr(chat_attempts, "record_audit", lambda *_a, **_k: None)

    result = chat_attempts.reconcile_attempt(
        db,
        owner_id=owner,
        attempt_id=attempt.id,
        resolution=SETTLE_CONSERVATIVE,
        evidence_ref="provider-receipt-2026-11-20",
        actor_id=owner,
    )

    assert events == [("settle", "reservation-identity", 120, Decimal("0.005"))]
    assert result.state == "settled"
    assert message.error_category == "external_effect_unresolved"
    assert db.commits == 1 and db.rollbacks == 0


# --- R15-03: explicit degraded grounding, local Ollama compatibility --------------------


def test_guarded_external_embedding_becomes_explicit_grounding_failure(monkeypatch):
    from app.rag import retrieve
    from app.rag.retrieve import GroundingUnavailableError

    captured = {}
    owner = uuid.uuid4()

    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *_a, **_k: (SimpleNamespace(name="openai"), "text-embedding-3-small"),
    )

    async def deny(*_args, **kwargs):
        captured.update(kwargs)
        raise ProviderError(
            "External query embedding requires a metered effect contract.",
            category="unsupported",
            provider_request_may_have_left=False,
        )

    monkeypatch.setattr(retrieve, "embed_with_policy", deny)

    with pytest.raises(GroundingUnavailableError) as error:
        asyncio.run(retrieve.retrieve_context(None, owner, "query", effect_guard=True))

    assert error.value.category == "unsupported"
    assert error.value.provider_request_may_have_left is False
    assert captured["effect_guard"] is True
    assert captured["owner_id"] == owner


def test_guarded_local_ollama_retrieval_preserves_owner_and_embedding_space(monkeypatch):
    from app.rag import retrieve

    captured = {}
    owner = uuid.uuid4()

    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *_a, **_k: (SimpleNamespace(name="ollama"), "nomic-embed-text"),
    )

    async def embed(_db, _provider, texts, **kwargs):
        captured["texts"] = texts
        captured.update(kwargs)
        return [[0.1, 0.2, 0.3]]

    def search(_db, owner_id, vector, **kwargs):
        captured["search_owner"] = owner_id
        captured["vector"] = vector
        captured.update(kwargs)
        return [{"text": "hit"}]

    monkeypatch.setattr(retrieve, "embed_with_policy", embed)
    monkeypatch.setattr(retrieve, "search", search)

    hits = asyncio.run(
        retrieve.retrieve_context(None, owner, "query", top_k=3, effect_guard=True)
    )

    assert hits == [{"text": "hit"}]
    assert captured["effect_guard"] is True
    assert captured["owner_id"] == owner
    assert captured["search_owner"] == owner
    assert captured["embedding_model"] == "nomic-embed-text"
    assert captured["embedding_native_dim"] == 3
