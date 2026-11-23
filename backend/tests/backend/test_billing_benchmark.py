"""Accounting overhead benchmark (P4).

Measures the per-call cost the Revenue & Compute Engine adds to a provider call:
one usage_log INSERT + two rollup UPSERTs (accounting), plus the in-process
reserve/settle pair (enforcement, in-memory store; the Redis path adds one pipeline
RTT per window — see the architecture doc). Prints measured numbers and asserts
generous ceilings so CI stays stable while still catching pathological regressions.
"""

import statistics
import time
import uuid
import warnings
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.audit  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.usage  # noqa: F401
import app.models.user  # noqa: F401
from app.billing import record_usage
from app.billing.enforcement import ComputeBudgetStore
from app.db import Base
from app.models.user import User, UserRole

warnings.filterwarnings("ignore")


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


def _timed(fn, n=200):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples), max(samples)


def test_accounting_overhead_benchmark(capsys):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    user = User(email="bench@x.example", password_hash="h", role=UserRole.member)
    db.add(user)
    db.commit()

    n_calls = [0]

    def accounting_call():
        n_calls[0] += 1
        record_usage(db, user_id=user.id, tenant_id=None, role="chat",
                     provider="openai" if n_calls[0] % 2 else "ollama",
                     model="gpt-4o-mini", prompt_tokens=500, completion_tokens=150,
                     agent="bench", task="bench")
        db.commit()  # chat commits per request; the benchmark includes the commit

    median_acc, max_acc = _timed(accounting_call, 100)

    store = ComputeBudgetStore(redis_url=None)
    caps_t, caps_c = (1_000_000, None), (int(1e9), None)  # high caps: 200 benchmark cycles must not exhaust

    def enforce_cycle():
        res = store.reserve(subject_type="user", subject_id="bench", estimated_tokens=2000,
                            estimated_cost_usd=Decimal("0.002"), token_caps=caps_t, cost_caps_micros=caps_c)
        store.settle(res, actual_tokens=650, actual_cost_usd=Decimal("0.00065"))

    median_enf, max_enf = _timed(enforce_cycle, 200)

    print(f"\naccounting (usage_log INSERT + 2 rollup UPSERTs + commit): median {median_acc:.2f} ms, max {max_acc:.2f} ms")
    print(f"enforcement (reserve+settle, in-process store): median {median_enf:.3f} ms, max {max_enf:.3f} ms")
    print(f"overhead vs a provider call (typically 300-3000 ms): "
          f"{median_acc + median_enf:.2f} ms — {100 * (median_acc + median_enf) / 300:.2f}% of the cheapest call")
    # Generous ceilings: the accounting write is one indexed insert + two upserts; if it
    # ever costs more than a real provider call's 1%, something is pathologically wrong.
    assert median_acc < 10.0, f"accounting overhead pathological: {median_acc:.2f} ms"
    assert median_enf < 2.0, f"enforcement overhead pathological: {median_enf:.3f} ms"
    db.close()
    engine.dispose()
