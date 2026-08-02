import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

logger = logging.getLogger("mainai.account")

from app.audit import record_audit
from app.cookies import clear_session_cookies
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models.user import User
from app.rag.account_erasure import AccountErasureBlockedError, erase_account_data
from app.rag.account_export import export_account_data
from app.schemas import DeleteAccountIn
from app.security import verify_password

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/export")
def export_account(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Thin wrapper: authenticates (via get_current_user), extracts neutral request metadata,
    and delegates the entire export to app/rag/account_export.py's export_account_data() — see
    that module's docstring for exactly what's included and why. No export-building logic
    lives here."""
    client_ip = request.client.host if request.client else None
    return export_account_data(db, user, client_ip=client_ip)


@router.delete("")
@limiter.limit("5/minute")
def delete_account(
    request: Request,
    response: Response,
    payload: DeleteAccountIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Permanent, irreversible deletion. Requires re-entering the current password: the
    session cookie alone (which is what every other authenticated endpoint relies on) isn't
    treated as sufficient proof of intent for a destructive, unrecoverable action.

    Thin wrapper (Pass 26): password verification, neutral request-metadata extraction,
    error-to-HTTP mapping, and cookie clearing are the ONLY things that happen here — the
    entire erasure sequence (S1A memory erasure, personal/shared data cleanup, durable
    storage-deletion tasks, the atomic transaction, the best-effort blob-deletion attempt) is
    app/rag/account_erasure.py's erase_account_data(). No duplicated erasure logic lives in
    this router."""
    if not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=user.id, action="account_deletion_failed_password", request=request)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Fel lösenord. Kontot har inte raderats.")

    client_ip = request.client.host if request.client else None
    user_id = user.id
    try:
        erase_account_data(db, user, client_ip=client_ip)
    except AccountErasureBlockedError as exc:
        # Not a failure -- nothing was changed (see erase_account_data's own docstring, the
        # blob-write-path audit). A distinct, actionable response rather than a generic 500.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        logger.exception("Kontoradering misslyckades för user_id=%s.", user_id)
        # Best-effort only, in a fresh transaction now that the failed one has already been
        # rolled back by erase_account_data() itself — if the DB is unreachable this write can
        # fail too, which is fine: the HTTP 500 and the exception log above are the actual
        # guarantee here, this is just supplementary visibility.
        try:
            record_audit(db, user_id=user_id, action="account_deletion_failed_error", request=request)
        except Exception:
            logger.exception("Kunde inte heller skriva audit-loggen för den misslyckade raderingen.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Kontot kunde inte raderas just nu. Inga ändringar gjordes — försök igen senare.",
        )

    # Cookies are only cleared AFTER erase_account_data() has confirmed its DB transaction
    # committed — never before, and never if it raised.
    clear_session_cookies(response)
    return {"status": "account_deleted"}
