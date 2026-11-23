"""Advertising security — adversarial suite (P3).

Attacks the ad subsystem the way a hostile client or a misconfigured deployment would:
consent races, forged/replayed engagement, cross-tenant probing, invalid settlements,
context manipulation and unauthorized administration. Everything fails CLOSED.
"""

import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.ads  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.user  # noqa: F401
from app.ads import service as ads_service
from app.ads.context import build_ad_context
from app.config import Settings
from app.db import Base
from app.models.ads import AdDecision, AdEvent, AdEventType
from app.models.user import User, UserRole

warnings.filterwarnings("ignore")


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    owner = User(email="adv-owner@x.example", password_hash="h", role=UserRole.member)
    other = User(email="adv-other@x.example", password_hash="h", role=UserRole.member)
    db.add_all([owner, other])
    db.commit()
    ads_service.configure_counters(None)
    yield db, owner, other
    db.close()
    engine.dispose()


def _s(**kw) -> Settings:
    base = dict(environment="development", ads_enabled=True,
                ads_house_ads_json='[{"id":"h1","title":"House ad","body":"Try Pro","url":"https://example.com"},'
                                   '{"id":"h2","title":"Second","body":"B","url":"https://example.com/2"}]')
    base.update(kw)
    return Settings(**base)


def _ctx(personalized=False, **kw):
    params = dict(placement="sidebar", day_part="morning", locale="en", tier="free",
                  personalized_allowed=personalized)
    params.update(kw)
    return build_ad_context(**params)


def _grant(db, user, personalized=False):
    ads_service.record_ad_consent(db, user, decision="granted", personalized=personalized,
                                  region="SE", policy_version="v1")
    db.commit()


# ---------------------------------------------------------------------------
# Consent withdrawal mid-stream
# ---------------------------------------------------------------------------

def test_withdrawal_between_impression_and_click_refuses_click(env):
    db, user, _o = env
    s = _s()
    _grant(db, user)
    sh = ads_service.session_hash_for(user.id, "n1")
    creative = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=sh, settings=s)
    assert creative is not None
    ads_service.record_ad_consent(db, user, decision="withdrawn", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    with pytest.raises(ValueError, match="consent"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id=creative["creative_id"], session_hash=sh,
                                    settings=s, now=datetime.now(timezone.utc) + timedelta(seconds=10))


def test_denial_after_grant_blocks_all_serving(env):
    db, user, _o = env
    s = _s()
    _grant(db, user)
    ads_service.record_ad_consent(db, user, decision="denied", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="x", settings=s) is None


# ---------------------------------------------------------------------------
# Forged impressions and clicks / replay attacks
# ---------------------------------------------------------------------------

def test_forged_click_unknown_creative_refused(env):
    db, user, _o = env
    s = _s()
    _grant(db, user)
    sh = ads_service.session_hash_for(user.id, "n")
    with pytest.raises(ValueError, match="no recent impression"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id="creative-we-invented", session_hash=sh, settings=s,
                                    now=datetime.now(timezone.utc) + timedelta(seconds=10))


def test_forged_session_hash_cannot_hijack_an_impression(env):
    db, user, _o = env
    s = _s(ads_click_min_interval_seconds=0)
    _grant(db, user)
    real_sh = ads_service.session_hash_for(user.id, "real-nonce")
    creative = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=real_sh, settings=s)
    assert creative is not None
    attacker_sh = ads_service.session_hash_for(user.id, "attacker-nonce")
    with pytest.raises(ValueError):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id=creative["creative_id"], session_hash=attacker_sh,
                                    settings=s, now=datetime.now(timezone.utc) + timedelta(seconds=10))


def test_click_replay_counted_exactly_once(env):
    """Replaying a valid click callback must be refused after the first count."""
    db, user, _o = env
    s = _s(ads_click_min_interval_seconds=0, ads_max_clicks_per_user_per_day=99)
    _grant(db, user)
    sh = ads_service.session_hash_for(user.id, "n")
    creative = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=sh, settings=s)
    t0 = datetime.now(timezone.utc)
    ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                creative_id=creative["creative_id"], session_hash=sh, settings=s,
                                now=t0 + timedelta(seconds=5))
    db.commit()
    with pytest.raises(ValueError, match="replay"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id=creative["creative_id"], session_hash=sh, settings=s,
                                    now=t0 + timedelta(seconds=30))


def test_daily_session_hash_rotation_defeats_hash_reuse(env):
    """The pseudonymous session hash rotates daily — a captured hash from yesterday is
    worthless today (impression windows are 1h, so no impression can be matched anyway)."""
    db, user, _o = env
    _grant(db, user)
    h_today = ads_service.session_hash_for(user.id, "nonce")
    import app.ads.service as svc

    original = svc.datetime

    class Tomorrow(original):
        @classmethod
        def now(cls, tz=None):
            return original.now(tz) + timedelta(days=1)

    svc.datetime = Tomorrow
    try:
        assert ads_service.session_hash_for(user.id, "nonce") != h_today
    finally:
        svc.datetime = original


def test_forged_impression_events_cannot_be_planted(env):
    """record_ad_event is the ONLY event entry point and it is consent+enabled gated;
    there is no path that plants impressions without a served placement."""
    db, user, _o = env
    off = _s(ads_enabled=False)
    with pytest.raises(ValueError):
        ads_service.record_ad_event(db, user, placement="sidebar", event="impression",
                                    creative_id="h1", session_hash="s", settings=off)
    no_consent = _s()
    with pytest.raises(ValueError, match="consent"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="impression",
                                    creative_id="h1", session_hash="s", settings=no_consent)


# ---------------------------------------------------------------------------
# Cross-tenant advertising data leakage
# ---------------------------------------------------------------------------

def test_ad_events_attribute_tenant_from_membership_not_input(env):
    db, owner, other = env
    tenant = __import__("app.models.billing", fromlist=["BillingTenant"]).BillingTenant(name="T", tier="paid")
    db.add(tenant)
    db.commit()
    owner.tenant_id = tenant.id
    db.add(owner)
    db.commit()
    s = _s()
    _grant(db, owner)
    sh = ads_service.session_hash_for(owner.id, "n")
    creative = ads_service.get_sponsored_placement(db, owner, _ctx(), session_hash=sh, settings=s)
    db.commit()
    assert creative is not None
    row = db.execute(select(AdEvent).limit(1)).scalar_one()
    assert row.tenant_id == tenant.id  # from membership, not from any client input
    # the OTHER user's events carry no trace of the first tenant
    _grant(db, other)
    ads_service.get_sponsored_placement(db, other, _ctx(), session_hash="o1", settings=s)
    db.commit()
    other_rows = db.execute(select(AdEvent).where(AdEvent.user_id == other.id)).scalars().all()
    assert all(r.tenant_id is None for r in other_rows)


def test_placement_payload_carries_no_cross_subject_data(env):
    db, user, other = env
    s = _s()
    _grant(db, user)
    payload = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=s)
    assert set(payload.keys()) == {"creative_id", "kind", "label", "title", "body", "url"}
    assert str(user.id) not in str(payload) and str(other.email) not in str(payload)


def test_ads_have_no_read_or_export_surface():
    """No endpoint may LIST ad events or consent rows (analytics exports are a future,
    separately-reviewed concern) — the router serves/records only."""
    backend = Path(__file__).resolve().parents[2] / "app" / "routers" / "ads.py"
    body = backend.read_text()
    assert "@router.get" not in body, "no GET endpoints: nothing may bulk-read ad data"


# ---------------------------------------------------------------------------
# Invalid revenue settlement
# ---------------------------------------------------------------------------

def test_settled_revenue_without_reference_is_unrepresentable(env):
    """DB CHECK: a settled row MUST carry a verified_ref — an advertiser (or a compromised
    admin path) cannot mark revenue settled by fiat."""
    db, _o, _t = env
    from app.models.billing import RevenueLedger, RevenueSource, RevenueStatus

    db.add(RevenueLedger(subject_type="platform", subject_id=None,
                         source=RevenueSource.ad_revenue, status=RevenueStatus.settled.value,
                         amount_micros=999_000_000,
                         period_start=datetime.now(timezone.utc).date().replace(day=1)))
    with pytest.raises(Exception):  # sqlite enforces CHECK constraints
        db.commit()
    db.rollback()


def test_estimated_revenue_never_enters_allocation_even_mixed(env):
    db, _o, _t = env
    from app.billing.allocation import platform_envelope
    from app.models.billing import RevenueLedger, RevenueSource, RevenueStatus

    now = datetime.now(timezone.utc)
    period = now.date().replace(day=1)
    db.add(RevenueLedger(subject_type="platform", subject_id=None, source=RevenueSource.ad_revenue,
                         status=RevenueStatus.estimated.value, amount_micros=5_000_000_000,
                         period_start=period))
    db.commit()
    env_p = platform_envelope(db, now=now)
    assert env_p.allocatable_micros == 0 and env_p.bound is False


# ---------------------------------------------------------------------------
# Ad-context manipulation
# ---------------------------------------------------------------------------

def test_hostile_context_values_are_safe(env):
    db, user, _o = env
    _grant(db, user)
    s = _s()
    # REJECTED at the firewall: structural violations (oversize, empty, control chars)
    for hostile in ("a" * 200, "", "\x00\x01", "line\nbreak"):
        with pytest.raises((TypeError, ValueError)):
            build_ad_context(placement=hostile, day_part="morning", locale="en",
                             tier="free", personalized_allowed=False)
        with pytest.raises((TypeError, ValueError)):
            build_ad_context(placement="sidebar", day_part="morning", locale=hostile,
                             tier="free", personalized_allowed=False)
    # INERT by design: injection-lookalike and traversal strings are plain scalars —
    # parameterized SQL and hashing mean they can do nothing. They pass through and
    # behave as opaque data.
    inert = build_ad_context(placement="'; DROP TABLE ad_events; --", day_part="morning",
                             locale="../../etc/passwd", tier="free", personalized_allowed=False)
    assert inert.placement == "'; DROP TABLE ad_events; --"
    assert ads_service.get_sponsored_placement(db, user, inert, session_hash="s", settings=s) is not None


def test_context_tier_cannot_be_spoofed_by_client(env):
    """The tier in the context comes from server-side membership, not client claims."""
    db, user, _o = env
    ctx = ads_service.build_ad_context(user, placement="sidebar", day_part="morning",
                                       locale="en", db=db)
    assert ctx.tier in ("free", "paid", "enterprise") and ctx.tier != ""


# ---------------------------------------------------------------------------
# Unauthorized administrative actions
# ---------------------------------------------------------------------------

def test_no_admin_surface_for_ads_or_billing_state():
    """Adversarial audit: NO route anywhere grants or revokes consent, mutates tenant
    caps, or settles revenue from a client request. Admin actions happen through
    operator-only channels (CLI/DB), which is the auditable path."""
    backend = Path(__file__).resolve().parents[2] / "app" / "routers"
    banned = ("BillingTenant(", "RevenueLedger(", "ads_service.record_ad_consent", "billing_tenants")
    offenders = []
    for p in backend.glob("*.py"):
        body = p.read_text()
        for b in banned:
            if b in body and p.name != "ads.py":
                offenders.append(f"{p.name}: {b}")
    assert offenders == [], f"unauthorized administration surfaces: {offenders}"
    # the ads router itself exposes consent ONLY for the authenticated user themselves
    ads_body = (backend / "ads.py").read_text()
    assert "user: User = Depends(get_current_user)" in ads_body
    assert ads_body.count("record_ad_consent") == 1


def test_consent_of_other_users_cannot_be_tampered_with(env):
    """record_ad_consent always writes for the AUTHENTICATED user; there is no user_id
    parameter to forge — attempt via kwargs must fail (TypeError)."""
    db, user, other = env
    with pytest.raises(TypeError):
        ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                      region="SE", policy_version="v1", user_id=other.id)
