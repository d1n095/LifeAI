import secrets
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cookies import ACCESS_COOKIE
from app.db import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.request_context import current_user_id as current_user_id_var
from app.security import decode_access_token
from app.token_revocation import is_access_token_revoked

CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    # Deliberately `async def`, not `def`: FastAPI runs sync dependencies in a worker
    # thread. contextvars.ContextVar.set() inside that thread would be local to that
    # thread's copy of the context and never become visible to the rest of the request —
    # current_user_id_var.set() below would silently do nothing. Running this as a
    # coroutine on the request's own task makes the mutation actually stick (see
    # app/request_context.py and the after_begin listener in app/db.py).
    #
    # Read from the HttpOnly cookie, not an Authorization header — see
    # docs/AUTH_THREAT_MODEL.md for why the token lives only in a cookie JavaScript can
    # never read, and why there is deliberately no header-based fallback path.
    access_token = request.cookies.get(ACCESS_COOKIE)
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inloggning krävs.")
    try:
        payload = decode_access_token(access_token)
        user_id = uuid.UUID(payload["sub"])
        jti = payload.get("jti")
        issued_at = payload.get("iat")
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ogiltig eller utgången token.") from exc

    # Catches the case a signature-valid, not-yet-expired JWT was explicitly revoked by a
    # logout or a reuse-detected family revocation — without this, "logout" would only stop
    # the session from renewing, not actually invalidate the still-live access token.
    if is_access_token_revoked(db, jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessionen har återkallats. Logga in igen.")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kontot finns inte eller är inaktiverat.")

    # Bulk revocation check: password reset and "log out everywhere" both just bump
    # sessions_valid_after (app/token_revocation.py) instead of blocklisting every
    # outstanding jti individually — any token issued at or before that timestamp is dead,
    # access token included, even though it's otherwise a signature-valid, unexpired JWT.
    #
    # <=, not <: JWT's iat (RFC 7519 NumericDate) has whole-second precision, so two events
    # that happen within the same wall-clock second are indistinguishable once compared —
    # there is no sub-second information left to order them by. Given that ambiguity, fail
    # closed (treat same-second as revoked) rather than fail open (treat it as still valid):
    # the failure mode of <= is "an extremely rare same-second session needs one extra
    # login", the failure mode of < would be "a same-second session survives a revocation
    # that was supposed to be immediate". app/security.py's utcnow_seconds() backdates
    # baseline (non-revocation) timestamps by a second precisely so a brand-new account's
    # own first login never collides with its own creation-time baseline under this rule.
    if issued_at is not None:
        issued_at_dt = datetime.fromtimestamp(issued_at, tz=timezone.utc)
        sessions_valid_after = user.sessions_valid_after.replace(tzinfo=timezone.utc)
        if issued_at_dt <= sessions_valid_after:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessionen har återkallats. Logga in igen.")

    # CSRF check, folded in here rather than a standalone cookie-comparing middleware:
    # frontend/backend are different origins, so the CSRF value can't live in a cookie the
    # frontend can read (see app/cookies.py) — it's delivered once via the login/refresh
    # response body and held in memory client-side, so verifying it requires the same DB
    # lookup this dependency already does. Every mutating route in this app requires
    # get_current_user (that's how authorization works at all), so this is complete
    # coverage without a separate middleware to keep in sync.
    if request.method not in SAFE_METHODS:
        # Looked up by access_jti, not filtered to non-revoked rows: a normal refresh
        # rotation revokes the OLD refresh-token row without invalidating the access token
        # still paired with it (which remains valid for its own natural lifetime) — the
        # CSRF value tied to that pairing must keep working until the access token itself
        # expires or is blocklisted, independent of refresh-token rotation state.
        row = db.query(RefreshToken).filter_by(access_jti=jti).first()
        header_value = request.headers.get(CSRF_HEADER)
        if row is None or not header_value or not secrets.compare_digest(row.csrf_token, header_value):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF-verifiering misslyckades.")

    # Bind this request to the user for Postgres Row-Level Security. The contextvar covers
    # any later transaction in this request (see the after_begin listener in app/db.py);
    # the explicit SET LOCAL below covers the transaction that's already open right now,
    # since after_begin already fired for it before we knew who the user was.
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    # Lets the rate limiter key on the authenticated user instead of just IP (app/limiter.py).
    request.state.user_id = user.id
    # Lets GET /api/auth/me hand the frontend its current CSRF value on every page load
    # (see app/routers/auth.py) — without this, a hard page reload would lose the
    # in-memory-only CSRF value until the next login/refresh.
    request.state.access_jti = jti

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Kräver adminbehörighet.")
    return user
