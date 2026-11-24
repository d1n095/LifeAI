"""Advertising subsystem isolation & compliance tests (P4).

Adversarial: these tests try to break the firewall the way a future contributor or a
misconfigured deployment might — passing private objects into the ad layer, wiring ad
content into reasoning paths, serving without consent, clicking without impressions,
and auto-clicking. Each attack must fail closed.
"""

import sys
import uuid
import warnings
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.ads  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.user  # noqa: F401
from app.ads import service as ads_service
from app.ads.context import AdContext, build_ad_context
from app.ads.sources import ConfiguredStaticSource, NullAdSource, SponsoredCreative, source_from_settings
from app.config import Settings
from app.db import Base
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
    user = User(email="ad@x.example", password_hash="h", role=UserRole.member)
    db.add(user)
    db.commit()
    ads_service.configure_counters(None)  # in-process counters for the test
    yield db, user, monkeypatch
    db.close()
    engine.dispose()


def _settings(**kw) -> Settings:
    base = dict(environment="development", ads_enabled=True,
                ads_house_ads_json='[{"id":"h1","title":"House ad","body":"Try Pro","url":"https://example.com"}]')
    base.update(kw)
    return Settings(**base)


def _ctx(tier="free", personalized=False):
    return build_ad_context(placement="sidebar", day_part="morning", locale="en",
                            tier=tier, personalized_allowed=personalized)


# ---------------------------------------------------------------------------
# Context firewall (adversarial)
# ---------------------------------------------------------------------------

def test_firewall_rejects_private_objects(env):
    """A future contributor tries to target ads on conversation/memory content: the
    firewall raises at construction — private data cannot even be CARRIED."""
    db, user, _mp = env
    from app.models.user import User as _U  # any ORM object must be rejected

    with pytest.raises(TypeError):
        AdContext(placement=user, day_part="morning", locale="en", tier="free", personalized_allowed=False)
    with pytest.raises((TypeError, ValueError)):
        build_ad_context(placement={"payload": "conversation text"}, day_part="morning",
                         locale="en", tier="free", personalized_allowed=False)
    with pytest.raises(ValueError):
        build_ad_context(placement="sidebar" * 50, day_part="morning", locale="en",
                         tier="free", personalized_allowed=False)


def test_ad_context_carries_no_private_fields():
    """The dataclass has EXACTLY the whitelisted fields — no content field can be snuck
    in without touching (and failing) this test."""
    fields = set(AdContext.__dataclass_fields__.keys())
    assert fields == {"placement", "day_part", "locale", "tier", "personalized_allowed"}
    for forbidden in ("conversation", "memory", "message", "prompt", "content", "query"):
        assert forbidden not in fields


def _imported_modules(path: Path) -> set[str]:
    """AST-based import scan — immune to comments and docstrings."""
    import ast

    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_dependency_direction_no_ads_in_reasoning_paths():
    """No reasoning/tool/authorization module may import app.ads; ads are presentation-
    only and must never reach prompts, tool execution or security decisions. AST-based,
    so a mention in a comment or docstring is NOT a violation."""
    backend = Path(__file__).resolve().parents[2] / "app"
    protected = ["mainai", "workforce", "providers", "egress_policy.py", "auth",
                 "deps.py", "openai_consult", "billing"]
    offenders = []
    for guard in protected:
        base = backend / guard
        files = base.rglob("*.py") if base.is_dir() else ([backend / guard] if (backend / guard).exists() else [])
        for p in files:
            if any(m == "app.ads" or m.startswith("app.ads.") or m == "app.routers.ads" for m in _imported_modules(p)):
                offenders.append(str(p.relative_to(backend)))
    assert offenders == [], f"reasoning/authorization modules importing the ad layer: {offenders}"
    # and conversely: the ad layer must not touch authorization machinery at all
    for p in (backend / "ads").rglob("*.py"):
        mods = _imported_modules(p)
        assert not any("execution_envelope" in m or "provider_spend" in m for m in mods), \
            "ads must not touch authorization machinery"


# ---------------------------------------------------------------------------
# Consent gating
# ---------------------------------------------------------------------------

def test_no_consent_no_placement(env):
    db, user, _mp = env
    s = _settings()
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=s) is None
    ads_service.record_ad_consent(db, user, decision="denied", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=s) is None


def test_withdrawal_takes_effect_immediately(env):
    db, user, _mp = env
    s = _settings()
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=s) is not None
    ads_service.record_ad_consent(db, user, decision="withdrawn", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s2", settings=s) is None


def test_personalized_serving_requires_explicit_personalized_consent(env):
    db, user, _mp = env
    s = _settings()
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    # a personalized context WITHOUT personalized consent must not be served
    assert ads_service.get_sponsored_placement(db, user, _ctx(personalized=True),
                                               session_hash="s", settings=s) is None


def test_disabled_by_default_serves_nothing_and_records_nothing(env):
    db, user, _mp = env
    off = _settings(ads_enabled=False)
    assert ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=off) is None
    with pytest.raises(ValueError):
        ads_service.record_ad_event(db, user, placement="sidebar", event="impression",
                                    creative_id="h1", session_hash="s", settings=off)


# ---------------------------------------------------------------------------
# Labeling (clearly-identified sponsored placements)
# ---------------------------------------------------------------------------

def test_unlabeled_creatives_are_refused(env):
    with pytest.raises(ValueError, match="sponsored"):
        SponsoredCreative(creative_id="x", kind="organic", label="", title="t", body="b", url="u")
    with pytest.raises(ValueError):
        SponsoredCreative(creative_id="x", kind="sponsored", label="", title="t", body="b", url="u")


def test_served_payload_is_clearly_labeled(env):
    db, user, _mp = env
    s = _settings()
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    payload = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash="s", settings=s)
    assert payload is not None
    assert payload["kind"] == "sponsored" and payload["label"] == "Sponsored"


def test_provider_agnostic_sources(env, monkeypatch):
    """NullAdSource by default; operator-supplied pool otherwise; the protocol is the
    only extension point (no ad-network SDK, no network calls)."""
    assert isinstance(source_from_settings(_settings(ads_house_ads_json=None)), NullAdSource)
    src = source_from_settings(_settings())
    assert isinstance(src, ConfiguredStaticSource)
    assert src.match(_ctx()).creative_id == "h1"
    import inspect
    from app.ads.sources import AdSource
    sig = inspect.signature(AdSource.match)
    assert list(sig.parameters) == ["self", "context"], "sources see ONLY the whitelisted context"


# ---------------------------------------------------------------------------
# Fraud / abuse controls
# ---------------------------------------------------------------------------

def test_impression_ceiling_enforced(env):
    db, user, _mp = env
    s = _settings(ads_max_impressions_per_user_per_day=3)
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    served = [ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=f"s{i}", settings=s)
              for i in range(5)]
    assert sum(1 for p in served if p is not None) == 3


def test_click_requires_plausible_prior_impression(env):
    db, user, _mp = env
    s = _settings()
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    sh = ads_service.session_hash_for(user.id, "nonce-1")
    creative = ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=sh, settings=s)
    assert creative is not None
    # wrong session hash -> refused
    with pytest.raises(ValueError, match="no recent impression"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id=creative["creative_id"],
                                    session_hash=ads_service.session_hash_for(user.id, "other"),
                                    settings=s)
    # correct session, implausibly fast -> refused
    with pytest.raises(ValueError, match="implausibly fast"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id=creative["creative_id"], session_hash=sh, settings=s)


def test_click_ceiling_enforced(env):
    db, user, _mp = env
    s = _settings(ads_max_clicks_per_user_per_day=2, ads_click_min_interval_seconds=0)
    ads_service.record_ad_consent(db, user, decision="granted", personalized=False,
                                  region="SE", policy_version="v1")
    db.commit()
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    for i in range(2):  # ceiling is 2: these two must pass
        sh = ads_service.session_hash_for(user.id, f"n{i}")
        ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=sh, settings=s, now=now)
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id="h1", session_hash=sh, settings=s, now=now + timedelta(seconds=5))
        db.rollback()  # keep the ledger small; counters are what matter here
    # third click: ceiling reached -> refused even with a valid impression
    sh = ads_service.session_hash_for(user.id, "n9")
    ads_service.get_sponsored_placement(db, user, _ctx(), session_hash=sh, settings=s, now=now)
    db.commit()
    with pytest.raises(ValueError, match="ceiling"):
        ads_service.record_ad_event(db, user, placement="sidebar", event="click",
                                    creative_id="h1", session_hash=sh, settings=s, now=now + timedelta(seconds=5))


def test_no_auto_click_code_path_exists():
    """Adversarial audit: NOTHING in the codebase clicks ads programmatically or offers
    engagement incentives. record_ad_event(event='click') must have exactly ONE caller —
    the explicit client-callback route."""
    backend = Path(__file__).resolve().parents[2] / "app"
    callers = []
    for p in backend.rglob("*.py"):
        text = p.read_text()
        if 'event="click"' in text or "event='click'" in text or "event=AdEventType.click" in text:
            callers.append(str(p.relative_to(backend)))
    assert callers == ["ads/service.py"], \
        f"click events must originate ONLY from the service layer, found: {callers}"
    # the HTTP route passes the client's event through the service (single entry point);
    # it must exist and be the ONLY HTTP surface calling record_ad_event
    route_text = (backend / "routers" / "ads.py").read_text()
    assert route_text.count("record_ad_event") == 1
    other_routers = [str(p.relative_to(backend)) for p in (backend / "routers").glob("*.py")
                     if p.name != "ads.py" and "record_ad_event" in p.read_text()]
    assert other_routers == [], f"ad engagement recorded outside the ads router: {other_routers}"
    # no incentivized-engagement vocabulary in CODE (docstrings legitimately name the
    # anti-goal, so the scan is AST-based with docstrings stripped)
    import ast

    def code_strings_and_names(path: Path) -> set[str]:
        tree = ast.parse(path.read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=True)
                if doc and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    node.body = node.body[1:]  # strip the docstring from the scan
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value.lower())
            elif isinstance(node, ast.Name):
                found.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                found.add(node.attr.lower())
        return found

    for p in (backend / "ads").rglob("*.py"):
        code = code_strings_and_names(p)
        for forbidden in ("autoclick", "auto_click", "rewarded", "click_reward", "incentivize"):
            assert not any(forbidden in token for token in code), \
                f"incentivized engagement term '{forbidden}' in CODE of {p.name}"


def test_session_hash_is_pseudonymous(env):
    db, user, _mp = env
    h1 = ads_service.session_hash_for(user.id, "nonce")
    h2 = ads_service.session_hash_for(user.id, "nonce")
    assert h1 == h2 and len(h1) == 64
    assert str(user.email) not in h1 and str(user.id) not in h1
    # rotates daily (different date component -> different hash)
    from datetime import datetime, timezone

    import app.ads.service as svc
    original = svc.datetime
    class FakeDT(original):
        @classmethod
        def now(cls, tz=None):
            return original.now(tz) + timedelta(days=1)
    svc.datetime = FakeDT
    try:
        assert ads_service.session_hash_for(user.id, "nonce") != h1
    finally:
        svc.datetime = original


from datetime import timedelta  # noqa: E402  (used by the pseudonymity test above)
