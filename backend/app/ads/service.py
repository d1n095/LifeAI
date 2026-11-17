"""Ad service: consent gating, clearly-labeled placements, fraud/abuse controls.

All decisions live HERE (sources are side-effect-free matchers). Serving an impression
requires, in order: ads_enabled → consent granted (latest event; personalized consent
for personalized serving) → daily impression ceiling not exhausted → a source match.
Every counter is a fail-closed store: when enabled in production, an unavailable counter
means NO impression is served (never serve uncounted). Clicks additionally require a
plausible prior impression for the same session hash + creative within a plausibility
window, honoring ads_click_min_interval_seconds.

There is deliberately NO auto-click or rewarded-engagement code path: click events
reach this module only through an explicit client callback, and nothing else calls
record_ad_event with event="click" (enforced by a test that greps the codebase).
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.models.ads import AdConsentEvent, AdDecision, AdEvent, AdEventType
from app.models.user import User

_PLACEMENT_MAX_PER_USER_PER_DAY = 0  # replaced by settings at call time


class AdsEnforcementUnavailableError(RuntimeError):
    """The ad-limit counter store is unavailable while ads are enabled. Fail-closed: no
    impression or click is recorded/served until the counter proves headroom."""


class _AdDayCounters:
    """Per-user, per-day event counters (impression/click/placement). Same storage stance
    as the rest of the platform: Redis when configured, process-local otherwise."""

    def __init__(self, redis_url: str | None):
        self._redis = None
        self._local: dict[str, int] = {}
        self._lock = threading.Lock()
        if redis_url:
            import redis as redis_module

            self._redis = redis_module.Redis.from_url(redis_url, decode_responses=True)

    def _key(self, user_id: str, kind: str, now: datetime) -> str:
        return f"ads:count:{user_id}:{kind}:{now.strftime('%Y%m%d')}"

    def _ttl(self, now: datetime) -> int:
        from app.openai_consult.budgets import _seconds_until_utc_midnight

        return _seconds_until_utc_midnight(now)

    def count(self, user_id: str, kind: str, now: datetime) -> int:
        key = self._key(user_id, kind, now)
        if self._redis is not None:
            try:
                return int(self._redis.get(key) or 0)
            except Exception as exc:
                import redis as redis_module

                if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
                    raise AdsEnforcementUnavailableError(
                        "ad-limit counter unavailable — no ad served (fail-closed)"
                    ) from exc
                raise
        with self._lock:
            return self._local.get(key, 0)

    def increment(self, user_id: str, kind: str, now: datetime) -> None:
        key = self._key(user_id, kind, now)
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, self._ttl(now))
                pipe.execute()
                return
            except Exception as exc:
                import redis as redis_module

                if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
                    raise AdsEnforcementUnavailableError(
                        "ad-limit counter unavailable — no ad served (fail-closed)"
                    ) from exc
                raise
        with self._lock:
            self._local[key] = self._local.get(key, 0) + 1


_counters = _AdDayCounters(None)


def configure_counters(redis_url: str | None) -> None:
    global _counters
    _counters = _AdDayCounters(redis_url)


def record_ad_consent(
    db: Session, user: User, *, decision: str, personalized: bool,
    region: str | None, policy_version: str,
) -> AdConsentEvent:
    """Append a consent decision. Withdrawal/denial take effect immediately because every
    serving decision reads the LATEST event."""
    if decision not in (AdDecision.granted.value, AdDecision.denied.value, AdDecision.withdrawn.value):
        raise ValueError(f"invalid ad consent decision: {decision!r}")
    event = AdConsentEvent(
        user_id=user.id,
        decision=decision,
        personalized=personalized and decision == AdDecision.granted.value,
        region=region,
        policy_version=policy_version,
    )
    db.add(event)
    record_audit(db, user_id=user.id, action="ad_consent_recorded",
                 entity_type="ad_consent", detail=f"{decision} personalized={personalized}")
    return event


def current_consent(db: Session, user: User) -> AdConsentEvent | None:
    latest = db.execute(
        select(AdConsentEvent)
        .where(AdConsentEvent.user_id == user.id)
        .order_by(AdConsentEvent.decided_at.desc(), AdConsentEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest


def build_ad_context(user: User, *, placement: str, day_part: str, locale: str,
                     db: Session | None = None, settings: Settings | None = None) -> "AdContext":
    from app.ads.context import build_ad_context as _build

    personalized = False
    if db is not None:
        consent = current_consent(db, user)
        personalized = bool(consent and consent.decision == AdDecision.granted.value and consent.personalized)
    return _build(
        placement=placement, day_part=day_part, locale=locale,
        tier="free" if user.tenant_id is None else "paid",
        personalized_allowed=personalized,
    )


def get_sponsored_placement(
    db: Session, user: User, context, *, session_hash: str,
    settings: Settings | None = None, source=None, now: datetime | None = None,
) -> dict | None:
    """Serve ONE clearly-labeled sponsored placement, or None. All gates fail-closed."""
    from app.ads.sources import source_from_settings

    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if not settings.ads_enabled:
        return None
    consent = current_consent(db, user)
    if consent is None or consent.decision != AdDecision.granted.value:
        return None
    if context.personalized_allowed and not consent.personalized:
        return None  # personalized serving needs personalized consent, always

    if _counters.count(str(user.id), "impression", now) >= settings.ads_max_impressions_per_user_per_day:
        record_audit(db, user_id=user.id, action="ad_impression_ceiling_reached")
        return None

    source = source or source_from_settings(settings)
    creative = source.match(context)
    if creative is None:
        return None

    _counters.increment(str(user.id), "impression", now)
    db.add(AdEvent(user_id=user.id, tenant_id=user.tenant_id, placement=context.placement,
                   event=AdEventType.impression.value, creative_id=creative.creative_id,
                   session_hash=session_hash, created_at=now))
    return {
        "creative_id": creative.creative_id,
        "kind": creative.kind,
        "label": creative.label,
        "title": creative.title,
        "body": creative.body,
        "url": creative.url,
    }


def record_ad_event(
    db: Session, user: User, *, placement: str, event: str, creative_id: str,
    session_hash: str, settings: Settings | None = None, now: datetime | None = None,
) -> AdEvent:
    """Record an impression or click. Clicks validate plausibility against a prior
    impression for the same session hash + creative. This is the ONLY entry point for
    engagement events and it is called exclusively from an explicit client callback."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if not settings.ads_enabled:
        raise ValueError("ads are disabled — no ad event may be recorded")
    consent = current_consent(db, user)
    if consent is None or consent.decision != AdDecision.granted.value:
        raise ValueError("no ad consent — no ad event may be recorded")

    if event == AdEventType.click.value:
        daily_clicks = _counters.count(str(user.id), "click", now)
        if daily_clicks >= settings.ads_max_clicks_per_user_per_day:
            record_audit(db, user_id=user.id, action="ad_click_ceiling_reached")
            raise ValueError("daily click ceiling reached")
        impression = db.execute(
            select(AdEvent)
            .where(
                AdEvent.user_id == user.id,
                AdEvent.session_hash == session_hash,
                AdEvent.creative_id == creative_id,
                AdEvent.event == AdEventType.impression.value,
                AdEvent.created_at >= now - timedelta(hours=1),
            )
            .order_by(AdEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if impression is None:
            record_audit(db, user_id=user.id, action="ad_click_without_impression_refused")
            raise ValueError("click refused: no recent impression for this session/creative")
        impression_at = impression.created_at
        if impression_at.tzinfo is None:  # sqlite returns naive datetimes
            impression_at = impression_at.replace(tzinfo=timezone.utc)
        elapsed = (now - impression_at).total_seconds()
        if elapsed < settings.ads_click_min_interval_seconds:
            record_audit(db, user_id=user.id, action="ad_click_too_fast_refused")
            raise ValueError("click refused: implausibly fast after impression")
        _counters.increment(str(user.id), "click", now)

    row = AdEvent(user_id=user.id, tenant_id=user.tenant_id, placement=placement,
                  event=event, creative_id=creative_id, session_hash=session_hash, created_at=now)
    db.add(row)
    return row


def session_hash_for(user_id, client_nonce: str) -> str:
    """Pseudonymous per-session identifier: SHA-256(user_id | nonce | date). No email,
    no IP, reversible by nobody without the nonce."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return hashlib.sha256(f"{user_id}|{client_nonce}|{day}".encode()).hexdigest()
