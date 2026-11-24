"""HTTP surface for the OpenAI consultation integration.

POST /api/openai-consult          — one gated consultation (founder-only, rate-limited).
GET  /api/openai-consult/status   — operator visibility WITHOUT secrets.

Error mapping (deliberately coarse — detail strings are founder-safe by construction):
    ConsultationConfigError   → 503 (integration off/unconfigured; fail-closed)
    ConsultationServiceError  → 503, except an explicit timeout category → 504
    BudgetExceededError       → 429 (per-user daily token/cost ceiling)
    EgressDeniedError         → 403 (content refused by the egress kernel — the whole
                                 call is denied, never retried with the same content)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.limiter import limiter
from app.models.user import User
from app.openai_consult.budgets import (
    BudgetEnforcementUnavailableError,
    BudgetExceededError,
    build_budget_store_from_settings,
)
from app.openai_consult.schemas import ConsultIn, ConsultOut, ConsultStatusOut
from app.openai_consult.service import (
    ConsultationConfigError,
    ConsultationServiceError,
    consult_openai,
    status_snapshot,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/openai-consult", tags=["openai-consult"], dependencies=[Depends(require_founder)])


@router.get("/status", response_model=ConsultStatusOut)
async def consult_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Configuration + remaining budget for the calling user. Carries no secret material
    (see ConsultStatusOut — there is no field that could hold the API key or a URL)."""
    try:
        store = build_budget_store_from_settings(settings)
    except BudgetEnforcementUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return status_snapshot(settings, store, user.id)


@router.post("", response_model=ConsultOut)
@limiter.limit(f"{settings.openai_consult_requests_per_minute}/minute")
async def consult(
    request: Request,
    payload: ConsultIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """One consultation through the official OpenAI Responses API.

    Disabled by default end to end: while OPENAI_CONSULT_ENABLED=false this endpoint always
    answers 503 without touching Redis, the budget store or the network."""
    try:
        budget_store = build_budget_store_from_settings(settings)
    except BudgetEnforcementUnavailableError as exc:
        # Production without Redis-backed distributed enforcement — or any environment
        # where the budget surface cannot be built: refuse before ANY admission decision.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        result, _stats = await consult_openai(
            db,
            owner_id=user.id,
            payload_in=payload,
            settings=settings,
            budget_store=budget_store,
            requested_by="routers.openai_consult.consult",
        )
    except BudgetEnforcementUnavailableError as exc:
        # Redis died at reserve/settle time: counter state unknown → fail closed (never
        # admit an uncounted call), clean 503 instead of a raw driver traceback.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConsultationConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Dagens {exc.which}-budget för konsultation är slut (fail-closed). Försök igen imorgon.",
        ) from exc
    except EgressDeniedError as exc:
        raise HTTPException(
            status_code=403,
            detail="Innehållet nekades av egress-policyn och skickas inte till någon extern "
            "tjänst (fail-closed).",
        ) from exc
    except ConsultationServiceError as exc:
        status_code = 504 if "kategori: timeout" in str(exc) else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return result
