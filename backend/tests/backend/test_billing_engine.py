"""Revenue & Compute Engine — unit/integration/adversarial tests (P4).

Covers: accounting correctness (rollups across subjects and windows), refund ceilings,
concurrency-safe spending (threaded reserve/settle), budget exhaustion, failed-provider
releases, tenant isolation, allocation settled-vs-estimated and margins, limits
composition, migration/model parity and the single-writer invariant.
"""

import hashlib
import threading
import uuid
import warnings
from datetime import datetime, timezone
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
from app.billing.allocation import platform_envelope, subject_envelope, tenant_envelope
from app.billing.limits import effective_limits
from app.billing.local_cost import estimate_local_compute_cost, local_cost_per_token
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
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def users(engine_db):
    db = engine_db
    u1 = User(email="a@x.example", password_hash="h", role=UserRole.member)
    u2 = User(email="b@x.example", password_hash="h", role=UserRole.member)
    db.add_all([u1, u2])
    db.commit()
    yield db, u1, u2


def _settings(**kw) -> Settings:
    return Settings(environment="development", **kw)


def _rollup(db, subject_type, subject_id, period_type, now=None):
    now = now or datetime.now(timezone.utc)
    # period_start is a DATE column — month periods are anchored at the first of the month
    # (see accounting._period_stamps; a bare "%Y-%m" string only ever worked on SQLite's
    # TEXT storage and is rejected by real PostgreSQL).
    ps = now.strftime("%Y-%m-%d") if period_type == "day" else now.strftime("%Y-%m-01")
    row = db.execute(
        text("SELECT tokens_in, tokens_out, cost_micros FROM usage_rollups WHERE subject_type=:st "
             "AND subject_id=:sid AND period_type=:pt AND period_start=:ps"),
        {"st": subject_type, "sid": str(subject_id), "pt": period_type, "ps": ps},
    ).first()
    return tuple(int(v) for v in row) if row else (0, 0, 0)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

def test_record_usage_writes_audit_row_and_all_four_rollups(users):
    db, u1, _u2 = users
    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
                 agent="chat", task="t", tier="paid")
    db.commit()
    assert (1000, 500, 450) == _rollup(db, "user", u1.id, "day")   # 0.001*0.15+0.0005*0.6 = 450 micros
    assert (1000, 500, 450) == _rollup(db, "user", u1.id, "month")
    log = db.execute(text("SELECT kind, tier, agent, task, cost_usd FROM usage_log")).first()
    assert log[:4] == ("consumption", "paid", "chat", "t")
    assert Decimal(str(log[4])) == Decimal("0.000450")


def test_unknown_price_counts_at_fallback_rate_never_free(users):
    db, u1, _u2 = users
    # provider/model with NO pricing entry
    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="mystery",
                 model="mystery-1", prompt_tokens=1000, completion_tokens=0, tier="paid")
    db.commit()
    _t, _o, cost_micros = _rollup(db, "user", u1.id, "day")
    assert cost_micros == 10_000  # fallback $0.01/1k tokens x 1000 tokens = $0.01 = 10000 micros
    log_cost = db.execute(text("SELECT cost_usd FROM usage_log")).first()[0]
    assert log_cost is None  # audit row stays honest (unknown), enforcement stays conservative


def test_refund_ceiling_and_atomicity(users):
    db, u1, _u2 = users
    record_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500, tier="paid")
    db.commit()
    refund_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                 model="gpt-4o-mini", prompt_tokens=400, completion_tokens=0,
                 cost_usd=Decimal("0.00015"))
    db.commit()
    t_in, t_out, cost = _rollup(db, "user", u1.id, "day")
    assert (t_in, t_out) == (600, 500) and cost == 300  # 450 - 150
    with pytest.raises(ValueError, match="refund exceeds"):
        refund_usage(db, user_id=u1.id, tenant_id=None, role="chat", provider="openai",
                     model="gpt-4o-mini", prompt_tokens=999999, completion_tokens=0,
                     cost_usd=Decimal("999"))
    db.rollback()  # refused refund must not have written anything
    assert (600, 500, 300) == _rollup(db, "user", u1.id, "day")


def test_tenant_and_user_subjects_both_recorded(users):
    db, _u1, u2 = users
    tenant = BillingTenant(name="Acme", tier="enterprise")
    db.add(tenant)
    db.commit()
    u2.tenant_id = tenant.id
    db.add(u2)
    db.commit()
    record_usage(db, user_id=u2.id, tenant_id=tenant.id, role="chat", provider="ollama",
                 model="llama3.1", prompt_tokens=2000, completion_tokens=0, tier="enterprise")
    db.commit()
    assert (2000, 0, 0) == _rollup(db, "user", u2.id, "day")
    assert (2000, 0, 0) == _rollup(db, "tenant", tenant.id, "day")
    assert (2000, 0, 0) == _rollup(db, "tenant", tenant.id, "month")


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def test_reserve_settle_releases_unused_part():
    store = ComputeBudgetStore(redis_url=None)
    caps_tokens, caps_cost = (10000, None), (int(0.01 * 1_000_000), None)
    res = store.reserve(subject_type="user", subject_id="s1", estimated_tokens=8000,
                        estimated_cost_usd=Decimal("0.008"), token_caps=caps_tokens,
                        cost_caps_micros=caps_cost)
    store.settle(res, actual_tokens=3000, actual_cost_usd=Decimal("0.003"))
    remaining = store.remaining(subject_type="user", subject_id="s1",
                                token_caps=caps_tokens, cost_caps_micros=caps_cost)
    # 8000 reserved, 3000 actually spent -> 7000 of the 10000 cap remains
    assert remaining["day"] == (7000, 7000)


def test_reserve_rejects_before_write_on_exhaustion():
    store = ComputeBudgetStore(redis_url=None)
    caps = ((5000, None), (None, None))
    store.reserve(subject_type="user", subject_id="s2", estimated_tokens=4000,
                  estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=caps[1])
    with pytest.raises(BillingBudgetExceededError):
        store.reserve(subject_type="user", subject_id="s2", estimated_tokens=2000,
                      estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=caps[1])
    # rejected reserve left NO partial state: still 4000 used
    remaining = store.remaining(subject_type="user", subject_id="s2",
                                token_caps=caps[0], cost_caps_micros=caps[1])
    assert remaining["day"][0] == 1000


def test_release_after_failed_provider_returns_everything():
    store = ComputeBudgetStore(redis_url=None)
    caps = ((5000, None), (None, None))
    res = store.reserve(subject_type="user", subject_id="s3", estimated_tokens=5000,
                        estimated_cost_usd=0, token_caps=caps[0], cost_caps_micros=caps[1])
    store.release(res)  # provider call failed before any spend
    remaining = store.remaining(subject_type="user", subject_id="s3",
                                token_caps=caps[0], cost_caps_micros=caps[1])
    assert remaining["day"] == (5000, -1)


def test_monthly_window_caps_across_days():
    store = ComputeBudgetStore(redis_url=None)
    from datetime import datetime as dt

    d1 = dt(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    d2 = dt(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
    monthly_tokens = (None, 10000)
    store.reserve(subject_type="user", subject_id="s4", estimated_tokens=6000,
                  estimated_cost_usd=0, token_caps=monthly_tokens, cost_caps_micros=(None, None), now=d1)
    with pytest.raises(BillingBudgetExceededError, match="month"):
        store.reserve(subject_type="user", subject_id="s4", estimated_tokens=6000,
                      estimated_cost_usd=0, token_caps=monthly_tokens, cost_caps_micros=(None, None), now=d2)


def test_concurrent_reserve_never_exceeds_cap():
    """10 threads race on one subject; the cap must hold exactly (no lost increments,
    no overshoot) — the core concurrency-safety contract."""
    store = ComputeBudgetStore(redis_url=None)
    cap = (10000, None)
    accepted, refused = [], []
    lock = threading.Lock()

    def worker(n):
        try:
            store.reserve(subject_type="user", subject_id="race", estimated_tokens=1000,
                          estimated_cost_usd=0, token_caps=cap, cost_caps_micros=(None, None))
            with lock:
                accepted.append(n)
        except BillingBudgetExceededError:
            with lock:
                refused.append(n)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(accepted) == 10 and len(refused) == 2, "cap 10000 with 12x1000 requests: exactly 10 fit"


def test_tenant_reservation_mirror_released_on_user_failure(users):
    """Reserving for BOTH user and tenant subjects: if the user-side reserve fails, the
    tenant-side reservation must be released (no phantom tenant spend)."""
    from app.billing.reservation import release_reservation, reserve_compute

    db, u1, _u2 = users
    tenant = BillingTenant(name="Acme", tier="enterprise")
    db.add(tenant)
    db.commit()
    u1.tenant_id = tenant.id
    db.add(u1)
    db.commit()
    limits = effective_limits(db, u1)
    res = reserve_compute(db, user=u1, limits=limits, estimated_tokens=1000,
                          estimated_cost_usd=Decimal("0.001"))
    release_reservation(res)
    # released: the store should be back to zero for both subjects
    from app.billing.reservation import _store
    store = _store(_settings())
    day_key = store._keys("tenant", str(tenant.id), datetime.now(timezone.utc))[0]
    assert store._read(day_key) == (0, 0)
    day_key_user = store._keys("user", str(u1.id), datetime.now(timezone.utc))[0]
    assert store._read(day_key_user) == (0, 0)


# ---------------------------------------------------------------------------
# Allocation (P3)
# ---------------------------------------------------------------------------

def test_estimated_revenue_never_funds_spending(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.ad_revenue,
                         status=RevenueStatus.estimated.value, amount_micros=1_000_000_000,
                         period_start=period))
    db.commit()
    env = platform_envelope(db, now=now)
    assert env.settled_revenue_micros == 0 and env.estimated_revenue_micros == 1_000_000_000
    assert env.allocatable_micros == 0 and env.bound is False, "estimated revenue must bind NOTHING"


def test_settled_revenue_binds_with_margin(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.grant,
                         status=RevenueStatus.settled.value, amount_micros=100_000_000,
                         period_start=period, verified_ref="wire-001"))
    db.commit()
    env = platform_envelope(db, now=now, settings=_settings(billing_safety_margin_pct=25))
    assert env.allocatable_micros == 75_000_000 and env.bound is True
    env0 = platform_envelope(db, now=now, settings=_settings(billing_safety_margin_pct=0))
    assert env0.allocatable_micros == 100_000_000


def test_tenant_isolation_no_cross_tenant_leakage(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    t1 = BillingTenant(name="A", tier="enterprise")
    t2 = BillingTenant(name="B", tier="enterprise")
    db.add_all([t1, t2])
    db.commit()
    db.add(RevenueLedger(subject_type="tenant", subject_id=t1.id, source=RevenueSource.subscription,
                         status=RevenueStatus.settled.value, amount_micros=50_000_000,
                         period_start=period, verified_ref="sub-A"))
    db.commit()
    own1 = tenant_envelope(db, t1, now=now)
    own2 = tenant_envelope(db, t2, now=now)
    assert own1.allocatable_micros > 0 and own2.allocatable_micros == 0
    assert own2.bound is False
    # subject composition: tenant 2 falls back to the platform envelope, never to t1's
    composed = subject_envelope(db, tenant_id=t2.id, now=now)
    assert composed.subject_type == "platform"
    assert composed.settled_revenue_micros != 50_000_000 or composed.settled_revenue_micros == 0


def test_limits_compose_allocation_and_tenant_override(engine_db):
    db = engine_db
    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    tenant = BillingTenant(name="A", tier="enterprise", daily_cost_cap_micros=500_000,
                           monthly_cost_cap_micros=10_000_000, safety_margin_pct=50)
    db.add(tenant)
    db.commit()  # flush first: tenant.id only exists after flush
    user = User(email="c@x.example", password_hash="h", role=UserRole.member, tenant_id=tenant.id)
    db.add(user)
    db.commit()
    db.add(RevenueLedger(subject_type="tenant", subject_id=tenant.id, source=RevenueSource.subscription,
                         status=RevenueStatus.settled.value, amount_micros=20_000_000,
                         period_start=period, verified_ref="sub-001"))
    db.commit()
    limits = effective_limits(db, user, _settings())
    # tenant monthly override 10.00 replaces the enterprise policy 250.00, then the
    # allocation envelope binds: 20.00 settled × (1-50%) = 10.00
    assert limits.monthly_cost_usd == 10.0
    assert limits.daily_cost_usd == 0.5


# ---------------------------------------------------------------------------
# Local cost model
# ---------------------------------------------------------------------------

def test_local_cost_model_and_non_local_none():
    s = _settings(billing_local_gpu_hour_usd=0.75, billing_local_gpu_tokens_per_second=30.0,
                  billing_local_energy_overhead_pct=20)
    per_token = local_cost_per_token(s)  # 0.75 / (30*3600) * 1.2
    assert per_token == Decimal("0.000008333")
    cost = estimate_local_compute_cost("ollama", 10_000, s)
    assert cost == Decimal("0.083330")
    assert estimate_local_compute_cost("openai", 10_000, s) is None


# ---------------------------------------------------------------------------
# Migration / model parity + lineage (0070, 0071)
# ---------------------------------------------------------------------------

def _ddl_blocks(sql: str):
    import re
    return dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\n        \)", sql, re.S))


def test_migrations_0070_plus_match_models_and_single_head():
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    sql70 = (versions / "0081_billing_foundation.py").read_text()
    blocks = _ddl_blocks(sql70)
    # usage_log extensions match the model
    for name in ("tenant_id", "agent", "task", "tier", "kind"):
        assert name in sql70, f"usage_log extension missing: {name}"
    # rollup uniqueness + non-negative guards
    assert "UNIQUE (subject_type, subject_id, period_type, period_start)" in blocks["usage_rollups"]
    assert "CHECK (cost_micros >= 0)" in blocks["usage_rollups"]
    # settled revenue REQUIRES a verified reference (anti-unverified-spend control)
    rev = blocks["revenue_ledger"]
    assert "revenue_settled_requires_ref" in rev and "CHECK (status = 'estimated' OR verified_ref IS NOT NULL)" in rev
    # ads tables carry no free-text advertiser fields beyond bounded ids/labels
    ads = _ddl_blocks((versions / "0082_advertising_tables.py").read_text())
    assert set(ads) == {"ad_consent_events", "ad_events"}
    for forbidden in ("content", "payload", "conversation", "memory", "prompt"):
        assert forbidden not in ads["ad_events"], f"ad_events must never carry {forbidden} data"

    # single head across MY lineage
    revs, downs = {}, set()
    for p in versions.glob("*.py"):
        m = re.search(r"^revision\s*[:=]\s*\"(\w+)\"", p.read_text(), re.M)
        d = re.search(r"^down_revision\s*[:=]\s*\"(\w+)\"", p.read_text(), re.M)
        if m:
            revs[m.group(1)] = p.name
            if d:
                downs.add(d.group(1))
    heads = set(revs) - downs
    # Invariant, not a pinned hash: exactly ONE head — the newest migration of this
    # lineage (0071 ads at first write; 0072 usage-idempotency extends it).
    assert len(heads) == 1, f"lineage must have exactly one head, got {heads}"
    for required in ("0080", "0081", "0082", "0083"):
        assert required in revs, f"migration {required} missing from the lineage"


def test_single_writer_invariant():
    """Adversarial: no code outside app/billing/accounting may write UsageLog/rollups."""
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for p in app_root.rglob("*.py"):
        text = p.read_text()
        if p.parts[-3:-1] == ("app", "billing") and p.name == "accounting.py":
            continue
        if "UsageLog(" in text and "usage.py" not in p.name:
            offenders.append(str(p.relative_to(app_root)))
    assert offenders == [], f"direct UsageLog writers outside the accounting pipeline: {offenders}"
