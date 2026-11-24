"""HTTP surface for founder account recovery — DELIBERATELY MINIMAL.

Only the CONFIRM step is exposed over HTTP: redeeming the single-use emailed link to the
new address. There is NO initiation endpoint — initiation is CLI-only
(`python -m app.founder_recovery initiate`) because the proof of account ownership must
include server access plus the recovery code, and a web endpoint would let a stolen code
alone start a takeover. The emailed link alone can change nothing.

The confirm endpoint is unauthenticated BY DESIGN: the single-use hashed token IS the
credential (same pattern as /reset-password). It re-verifies every gate at confirm time
(feature still enabled, code still configured, account still the pinned founder row),
so a link minted before the feature was switched off cannot complete.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.founder_recovery import FounderRecoveryError, confirm_founder_email_change
from app.limiter import limiter

router = APIRouter(prefix="/api/auth/founder-recovery", tags=["auth"])

settings = get_settings()

# Same neutral-response posture as /reset-password: identical bodies for every failure
# class so nothing about the request state leaks to an unauthenticated caller.
_NEUTRAL_FAILURE = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Länken är ogiltig eller har gått ut. Begär en ny återställning via servern.",
)


class ConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)


@router.post("/confirm")
@limiter.limit(f"{settings.rate_limit_reset_password_per_minute}/minute")
def confirm_recovery(request: Request, payload: ConfirmIn, db: Session = Depends(get_db)):
    try:
        new_email = confirm_founder_email_change(db, token_plain=payload.token)
    except FounderRecoveryError as exc:
        # Every refusal maps to the same neutral detail; the specific reason exists only
        # in the server-side audit trail / CLI output — never here.
        raise _NEUTRAL_FAILURE from exc
    return {"status": "email_recovered", "email": new_email}
