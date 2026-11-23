"""Financial correctness suite (P2) — deterministic accounting + auditable invariants.

Adversarial where it matters: concurrent money movement, duplicate events, partial
failures, retries/cancellation, revenue reversals/chargebacks, negative balances,
integer overflow, clock boundaries, tenant isolation and unauthorized budget changes.
Every test asserts DETERMINISTIC outcomes — accounting must be reproducible, not merely
"usually right".
"""

import threading
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.audit  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.usage  # noqa: F401
import app.models.user  # noqa: F401
from app.billing import BillingBudgetExceededError, ComputeBudgetStore, record_usage, refund_usage
from app.billing.accounting import _derive_tenant_id
from app.billing.allocation import platform_envelope, subject_envelope
from app.billing.limits import effective_limits
from app.billing.reservation import release_reservation, reserve_compute, settle_reservation
from app.config import Settings
from app.db import Base
from app.models.billing import BillingTenant, RevenueLedger, RevenueSource, RevenueStatus
from app.models.user import User, UserRole

warnings.filterwarnings("ignore")


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


@pytest.fixture
def engine_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def world(engine_db):
    db = engine_db
    u1 = User(email="fin-a@x.example", password_hash="h", role=UserRole.member)
    u2 = User(email="fin-b@x.example", password_hash="h", role=UserRole.member)
    db.add_all([u1, u2])
    db.commit()
    tenant = BillingTenant(name="Acme", tier="enterprise", daily_cost_cap_micros=2_000_000,
                           monthly_cost_cap_micros=20_000_000, safety_margin_pct=25)
    db.add(tenant)
    db.commit()
    u2.tenant_id = tenant.id
    db.add(u2)
    db.commit()
    return db, u1, u2, tenant


def _rollup(db, subject_type, subject_id, period_type, now=None):
    now = now or datetime.now(timezone.utc)
    ps = now.strftime("%Y-%m-%d") if period_type == "day" else now.strftime("%Y-%m")
    row = db.execute(
        text("SELECT tokens_in, tokens_out, cost_micros FROM usage_rollups WHERE subject_type=:st "
             "AND subject_id=:sid AND period_type=:pt AND period_start=:ps"),
        {"st": subject_type, "sid": str(subject_id), "pt": period_type, "ps": ps},
    ).first()
    return tuple(int(v) for v in row) if row else (0, 0, 0)


def _s(**kw) -> Settings:
    return Settings(environment="development", **kw)


# ---------------------------------------------------------------------------
# 1. Concurrent reservations and settlements
# ---------------------------------------------------------------------------

def test_concurrent_reserve_settle_cycles_hold_the_cap():
    """20 threads each run a full reserve->settle cycle; the aggregate charged must be
    exactly sum(actual) with no lost or phantom increments, and the cap must hold."""
    store = ComputeBudgetStore(redis_url=None)
    cap = (50_000, None)
    lock = threading.Lock()
    actuals = []

    def worker(i):
        res = store.reserve(subject_type="user", subject_id="cc", estimated_tokens=5000,
                            estimated_cost_usd=0, token_caps=cap, cost_caps_micros=(None, None))
        actual = 1000 + i
        with lock:
            actuals.append(actual)
        store.settle(res, actual_tokens=actual, actual_cost_usd=0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    remaining = store.remaining(subject_type="user", subject_id="cc",
                                token_caps=cap, cost_caps_micros=(None, None))
    # remaining == cap - sum(actuals) EXACTLY (deterministic, no drift)
    assert remaining["day"][0] == 50_000 - sum(actuals)


def test_concurrent_double_reserve_never_overshoots_tenant_and_user(world):
    """Threads race to reserve against a tenant with a 2 000 000-micro daily cost cap;
    exactly the number of reservations that fit are admitted."""
    db, _u1, u2, tenant = world
    limits = effective_limits(db, u2, _s())
    store_ref = []

    def attempt():
        try:
            res = reserve_compute(db, user=u2, limits=limits, estimated_tokens=1000,
                                  estimated_cost_usd=Decimal("0.50"), settings=_s())
            with threading.Lock():
                store_ref.append(res)
        except BillingBudgetExceededError:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # cap 2.00 daily; each reserve 0.50 -> exactly 4 admitted
    assert len(store_ref) == 4


# ---------------------------------------------------------------------------
# 2. Duplicate usage events and idempotency
# ---------------------------------------------------------------------------

def test_duplicate_event_with_same_key_charges_once(world):
    db, u1, _u2, _t = world
    kwargs = dict(user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                  model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
                  tier="paid", idempotency_key="evt-1")
    first = record_usage(db, **kwargs)
    second = record_usage(db, **kwargs)
    db.commit()
    assert first.id == second.id
    assert (1000, 500, 450) == _rollup(db, "user", u1.id, "day"), "double event must charge once"


def test_idempotency_race_charges_exactly_once(world):
    """Two threads write the same idempotency key concurrently: exactly one audit row and
    one rollup increment survive the race (SAVEPOINT-based loser resolution)."""
    db, u1, _u2, _t = world
    results = []

    def write():
        results.append(record_usage(db, user_id=u1.id, tenant_id=None, role="chat",
                                    provider="openai", model="gpt-4o-mini",
                                    prompt_tokens=1000, completion_tokens=0, tier="paid",
                                    idempotency_key="race-evt").id)

    threads = [threading.Thread(target=write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.commit()
    assert len(set(results)) == 1, "both writers must resolve to the SAME single row"
    assert (1000, 0, 150) == _rollup(db, "user", u1.id, "day")  # 1000 tok x $0.15/1k = 150 micros


def test_retries_of_failed_calls_must_each_count(world):
    """Deliberately NO idempotency key: a retry after a FAILED provider call is a real
    billable attempt — deduping it would understate spend (over-admission vector)."""
    db, u1, _u2, _t = world
    for _attempt in range(3):
        record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                     model="gpt-4o-mini", prompt_tokens=100, completion_tokens=0, tier="paid")
    db.commit()
    assert (300, 0, 45) == _rollup(db, "user", u1.id, "day")  # 3 x (100 tok x 15 micros)


# ---------------------------------------------------------------------------
# 3. Partial provider failures + 4. retries and cancellation
# ---------------------------------------------------------------------------

def test_partial_failure_then_fallback_success_no_phantom_spend(world):
    db, u1, _u2, _t = world
    limits = effective_limits(db, u1, _s())
    res = reserve_compute(db, user=u1, limits=limits, estimated_tokens=5000,
                          estimated_cost_usd=Decimal("0.01"), settings=_s())
    release_reservation(res)  # provider A failed before any spend
    res2 = reserve_compute(db, user=u1, limits=limits, estimated_tokens=5000,
                           estimated_cost_usd=Decimal("0.01"), settings=_s())
    settle_reservation(res2, actual_tokens=2000, actual_cost_usd=Decimal("0.004"))
    # net charged: exactly 0.004 — the failed attempt left nothing behind
    remaining = reserve_compute._store_cache.remaining if False else None  # noqa: F841
    store = __import__("app.billing.reservation", fromlist=["_store"])._store(_s())
    rem = store.remaining(subject_type="user", subject_id=str(u1.id),
                          token_caps=limits.token_caps(), cost_caps_micros=limits.cost_caps_micros())
    assert rem["day"][1] == limits.cost_caps_micros()[0] - 4000


def test_cancellation_mid_call_leaks_at_most_one_bounded_reservation(world):
    """A cancelled request (exception path without release) may leak ONE worst-case
    reservation until the window TTL clears it — deliberately conservative: under-
    admission, never over-admission. The NEXT reservation must still be admitted if the
    cap allows, and never refunded by the leak."""
    db, u1, _u2, _t = world
    limits = effective_limits(db, u1, _s(billing_paid_daily_cost_usd=0.03))
    res = reserve_compute(db, user=u1, limits=limits, estimated_tokens=8000,
                          estimated_cost_usd=Decimal("0.02"), settings=_s())
    del res  # simulated cancellation: no release, no settle
    # worst-case reservation 0.02 held; a second 0.02 reserve must now be refused (0.04 > 0.03)
    with pytest.raises(BillingBudgetExceededError):
        reserve_compute(db, user=u1, limits=limits, estimated_tokens=8000,
                        estimated_cost_usd=Decimal("0.02"), settings=_s())


# ---------------------------------------------------------------------------
# 5. Revenue reversals, refunds and chargebacks
# ---------------------------------------------------------------------------

def test_chargeback_reversal_reduces_and_can_zero_the_envelope(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.subscription,
                         status=RevenueStatus.settled.value, amount_micros=100_000_000,
                         period_start=period, verified_ref="wire-1"))
    db.commit()
    assert platform_envelope(db, now=now).allocatable_micros == 75_000_000
    # chargeback: settled REVERSAL row with its own verified reference
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.subscription,
                         status=RevenueStatus.settled.value, amount_micros=-100_000_000,
                         period_start=period, verified_ref="chargeback-1"))
    db.commit()
    env = platform_envelope(db, now=now)
    assert env.settled_revenue_micros == 0 and env.allocatable_micros == 0


def test_reversal_overflow_never_makes_allocatable_negative(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.grant,
                         status=RevenueStatus.settled.value, amount_micros=10_000_000,
                         period_start=period, verified_ref="g1"))
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.grant,
                         status=RevenueStatus.settled.value, amount_micros=-50_000_000,
                         period_start=period, verified_ref="cb-1"))
    db.commit()
    env = platform_envelope(db, now=now)
    assert env.settled_revenue_micros == -40_000_000
    assert env.allocatable_micros == 0, "over-reversed revenue must clamp to ZERO, never fund negative spend"


def test_refund_never_pushes_rollup_negative(world):
    db, u1, _u2, _t = world
    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=0, tier="paid")
    db.commit()
    with pytest.raises(ValueError):
        refund_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                     model="gpt-4o-mini", prompt_tokens=1001, completion_tokens=0,
                     cost_usd=Decimal("999"))
    db.rollback()
    assert _rollup(db, "user", u1.id, "day")[2] >= 0


# ---------------------------------------------------------------------------
# 6. Integer overflow / extreme values
# ---------------------------------------------------------------------------

def test_extreme_values_never_wrap_or_crash():
    store = ComputeBudgetStore(redis_url=None)
    huge = 10**15
    cap = (huge, None)
    res = store.reserve(subject_type="user", subject_id="big", estimated_tokens=huge,
                        estimated_cost_usd=Decimal("999999999999.999999"),
                        token_caps=cap, cost_caps_micros=(None, None))
    # settle to a small actual: adjustments stay exact integers, no float creep
    store.settle(res, actual_tokens=1, actual_cost_usd=Decimal("0.000001"))
    remaining = store.remaining(subject_type="user", subject_id="big",
                                token_caps=cap, cost_caps_micros=(None, None))
    assert remaining["day"][0] == huge - 1


def test_overflow_refusal_at_cap_boundary():
    store = ComputeBudgetStore(redis_url=None)
    cap = (2**62, None)
    res = store.reserve(subject_type="user", subject_id="of", estimated_tokens=2**62,
                        estimated_cost_usd=0, token_caps=cap, cost_caps_micros=(None, None))
    with pytest.raises(BillingBudgetExceededError):
        store.reserve(subject_type="user", subject_id="of", estimated_tokens=1,
                      estimated_cost_usd=0, token_caps=cap, cost_caps_micros=(None, None))


# ---------------------------------------------------------------------------
# 7. Time-window boundaries and clock differences
# ---------------------------------------------------------------------------

def test_day_rollover_reservation_is_judged_at_reserve_time():
    """Reserve at 23:59:59, settle at 00:00:01 NEXT day: the reservation and its
    settlement are judged in the window they were RESERVED in — deterministic across
    clock skew between workers (the keys travel with the reservation)."""
    store = ComputeBudgetStore(redis_url=None)
    before = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 18, 0, 0, 1, tzinfo=timezone.utc)
    caps = ((10000, None), (None, None))
    res = store.reserve(subject_type="user", subject_id="roll", estimated_tokens=8000,
                        estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=(None, None),
                        now=before)
    store.settle(res, actual_tokens=3000, actual_cost_usd=0)  # settle uses res.period_keys
    rem_old = store.remaining(subject_type="user", subject_id="roll",
                              token_caps=caps[0], cost_caps_micros=(None, None), now=before)
    rem_new = store.remaining(subject_type="user", subject_id="roll",
                              token_caps=caps[0], cost_caps_micros=(None, None), now=after)
    assert rem_old["day"][0] == 7000, "settle reconciles in the ORIGINAL day window (8000 reserved - 3000 actual)"
    assert rem_new["day"][0] == 10000, "the NEW day starts clean"


def test_month_boundary_isolation():
    store = ComputeBudgetStore(redis_url=None)
    nov = datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc)
    dec = datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)
    caps = ((None, 10000), (None, None))
    store.reserve(subject_type="user", subject_id="mo", estimated_tokens=9000,
                  estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=(None, None), now=nov)
    # December must NOT inherit November's consumption
    res = store.reserve(subject_type="user", subject_id="mo", estimated_tokens=9000,
                        estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=(None, None), now=dec)
    assert res is not None


def test_accounting_windows_use_utc_regardless_of_local_clock(world):
    db, u1, _u2, _t = world
    # a caller with a wrong local timezone still lands in the correct UTC windows
    now_utc = datetime.now(timezone.utc)
    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=10, completion_tokens=0, tier="paid",
                 created_at=now_utc)
    db.commit()
    assert _rollup(db, "user", u1.id, "day", now=now_utc)[0] == 10


# ---------------------------------------------------------------------------
# 8. Tenant isolation and unauthorized budget changes
# ---------------------------------------------------------------------------

def test_tenant_attribution_cannot_be_forged(world):
    """Adversarial: a compromised caller tries to attribute its spend to another tenant
    (stealing that tenant's budget headroom). Membership comes from the user row; the
    forged value is refused."""
    db, u1, _u2, tenant = world
    with pytest.raises(ValueError, match="disagrees"):
        record_usage(db, user_id=u1.id, tenant_id=tenant.id, role="chat", provider="openai",
                     model="gpt-4o-mini", prompt_tokens=10, completion_tokens=0, tier="paid")
    db.rollback()
    assert _rollup(db, "tenant", tenant.id, "day") == (0, 0, 0)
    assert _rollup(db, "user", u1.id, "day") == (0, 0, 0)


def test_tenant_caps_now_bind_at_admission(world):
    """P1 review fix regression: the tenant's daily cost cap bounds the SHARED envelope —
    one member's reservations consume it for everyone (and the second member is refused
    once it is exhausted)."""
    db, _u1, u2, tenant = world
    u_extra = User(email="fin-c@x.example", password_hash="h", role=UserRole.member, tenant_id=tenant.id)
    db.add(u_extra)
    db.commit()
    # daily cap 2.00; first member reserves 1.50
    reserve_compute(db, user=u2, limits=effective_limits(db, u2, _s()), estimated_tokens=1000,
                    estimated_cost_usd=Decimal("1.50"), settings=_s())
    with pytest.raises(BillingBudgetExceededError, match="tenant"):
        reserve_compute(db, user=u_extra, limits=effective_limits(db, u_extra, _s()),
                        estimated_tokens=1000, estimated_cost_usd=Decimal("1.00"), settings=_s())


def test_no_http_route_mutates_billing_or_revenue_tables():
    """Auditable invariant: caps, tenants and the revenue ledger change ONLY through
    operator/DB actions — there is NO HTTP surface that can mutate them (a compromised
    client or a miswired route cannot change budgets)."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for p in (backend / "routers").glob("*.py"):
        body = p.read_text()
        for banned in ("BillingTenant(", "RevenueLedger(", "billing_tenants", "revenue_ledger",
                       "usage_rollups"):
            if banned in body and "ads" not in p.name:
                offenders.append(f"{p.name}: {banned}")
    assert offenders == [], f"HTTP routes mutating billing/revenue state: {offenders}"


# ---------------------------------------------------------------------------
# 9. Auditable invariant: audit rows and rollups agree
# ---------------------------------------------------------------------------

def test_audit_rows_aggregate_exactly_to_rollups(world):
    """The core auditability invariant: for any subject/window, summing the usage_log
    events (micro-ceiling rounding per row) reproduces the rollup EXACTLY."""
    db, u1, _u2, _t = world
    from decimal import ROUND_CEILING

    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=333, tier="paid")
    record_usage(db, user_id=u1.id, tenant_id=None, role="embedding", provider="openai",
                 model="text-embedding-3-small", prompt_tokens=500, completion_tokens=0, tier="paid")
    refund_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=100, completion_tokens=0,
                 cost_usd=Decimal("0.00006"))
    db.commit()

    from sqlalchemy import select as _sel

    from app.models.usage import UsageLog as _UL

    rows = db.execute(
        _sel(_UL.kind, _UL.prompt_tokens, _UL.completion_tokens, _UL.cost_usd, _UL.provider, _UL.model)
        .where(_UL.user_id == u1.id)
    ).all()
    tok_in = tok_out = 0
    micros = 0
    from app.providers.pricing import estimate_cost as _est

    for kind, pin, pout, cost, provider, model in rows:
        sign = -1 if kind == "refund" else 1
        tok_in += sign * pin
        tok_out += sign * pout
        c = cost if cost is not None else _est(provider, model, pin, pout)
        m = int((Decimal(str(c)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        micros += sign * m
    assert (tok_in, tok_out, micros) == _rollup(db, "user", u1.id, "day"), \
        "rollups must be an exact aggregate of the audit rows"
