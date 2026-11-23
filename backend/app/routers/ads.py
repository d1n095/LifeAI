"""Advertising HTTP surface (minimal, rate-limited, disabled by default).

No `from __future__ import annotations` here on purpose: PEP 563 turns parameter
annotations into strings and FastAPI resolves them against the wrapper function's module
(app.limiter, via @limiter.limit), where the request models don't exist. The rest of the
routers follow the same convention.

Three endpoints, all explicitly gated on ads_enabled (404-style refusals when disabled —
the surface must be INVISIBLE, not merely inert):

* POST /api/ads/consent            — record a consent decision (grant/deny/withdraw).
* POST /api/ads/placement          — fetch ONE clearly-labeled sponsored slot (or null).
* POST /api/ads/event              — record an impression/click callback (clicks are
                                     validated for plausibility; NO auto-click exists).

The endpoints accept NO free-text targeting input beyond placement/locale scalars; the
AdContext firewall (app/ads/context.py) type-rejects anything else. No conversation,
memory, or agent-context data reaches this module by design — see app/ads/__init__.py
and tests/backend/test_ads_isolation.py.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ads import service as ads_service
from app.ads.context import build_ad_context
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models.user import User

router = APIRouter(prefix="/api/ads", tags=["ads"])


class AdConsentIn(BaseModel):
    decision: str = Field(pattern="^(granted|denied|withdrawn)$")
    personalized: bool = False
    region: str | None = Field(default=None, max_length=8)
    policy_version: str = Field(max_length=32)


class AdEventIn(BaseModel):
    placement: str = Field(max_length=64)
    event: str = Field(pattern="^(impression|click)$")
    creative_id: str = Field(max_length=128)
    session_nonce: str = Field(max_length=128)


class AdPlacementIn(BaseModel):
    placement: str = Field(max_length=64)
    locale: str = Field(max_length=16, default="en")


def _require_ads_enabled() -> None:
    if not get_settings().ads_enabled:
        # 404, not 403: an endpoint that is merely forbidden still advertises that an ad
        # system exists; a disabled one should be indistinguishable from absent.
        raise HTTPException(status_code=404, detail="Not Found")


@router.post("/consent")
@limiter.limit("10/minute")
def post_consent(request: Request, payload: AdConsentIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    _require_ads_enabled()
    event = ads_service.record_ad_consent(
        db, user,
        decision=payload.decision, personalized=payload.personalized,
        region=payload.region, policy_version=payload.policy_version,
    )
    db.commit()
    return {"decision": event.decision, "personalized": event.personalized,
            "policy_version": event.policy_version}


@router.post("/placement")
@limiter.limit("30/minute")
def post_placement(request: Request, payload: AdPlacementIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    _require_ads_enabled()
    context = build_ad_context(
        user, placement=payload.placement, day_part=_day_part(), locale=payload.locale, db=db
    )
    nonce = uuid.uuid4().hex
    session_hash = ads_service.session_hash_for(user.id, nonce)
    creative = ads_service.get_sponsored_placement(
        db, user, context, session_hash=session_hash
    )
    db.commit()
    # `creative` is None whenever any gate refuses (disabled, no consent, ceiling, no
    # source) — the response shape makes "no ad" a first-class, quiet outcome. The
    # session nonce is returned so a click callback can echo it (plausibility check);
    # it identifies the SESSION, not the user, and rotates daily.
    return {"sponsored": creative, "session_nonce": nonce}


@router.post("/event")
@limiter.limit("60/minute")
def post_event(request: Request, payload: AdEventIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    _require_ads_enabled()
    try:
        ads_service.record_ad_event(
            db, user,
            placement=payload.placement, event=payload.event,
            creative_id=payload.creative_id,
            session_hash=ads_service.session_hash_for(user.id, payload.session_nonce),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    return {"recorded": payload.event}


def _day_part() -> str:
    import datetime as _dt

    hour = _dt.datetime.now(_dt.timezone.utc).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"
