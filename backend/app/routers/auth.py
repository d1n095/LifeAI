import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.cookies import REFRESH_COOKIE, clear_session_cookies, set_session_cookies
from app.db import get_db
from app.deps import CSRF_HEADER, get_current_user
from app.limiter import limiter
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas import LoginIn, SessionOut
from app.security import (
    create_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.token_revocation import revoke_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()


def _issue_session(
    db: Session, request: Request, response: Response, user: User, family_id: uuid.UUID
) -> tuple[RefreshToken, str]:
    """Create a new refresh token row (in the given family) plus a fresh access token and
    CSRF value, and attach the two session cookies. Shared by login (new family) and
    refresh (existing family, see the rotation logic in the refresh endpoint below).
    Returns the new row and the plaintext CSRF value for the caller to put in the response
    body (the only time it's ever transmitted — see docs/AUTH_THREAT_MODEL.md)."""
    refresh_plain = generate_refresh_token()
    access_token, access_jti = create_access_token(user.id, user.role.value)
    csrf_token = generate_csrf_token()

    new_row = RefreshToken(
        user_id=user.id,
        family_id=family_id,
        token_hash=hash_refresh_token(refresh_plain),
        access_jti=access_jti,
        csrf_token=csrf_token,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    db.add(new_row)
    db.commit()
    # No db.refresh(): `id` is populated client-side by the default=uuid.uuid4 (see
    # app/models/refresh_token.py) — same reasoning as app/routers/chat.py.

    set_session_cookies(response, access_token, refresh_plain)
    return new_row, csrf_token


def _session_response(user: User, csrf_token: str) -> SessionOut:
    return SessionOut(id=user.id, email=user.email, role=user.role.value, csrf_token=csrf_token)


def _verify_csrf(request: Request, row: RefreshToken) -> None:
    """Used by /refresh and /logout, which authenticate via the refresh-token cookie rather
    than get_current_user (the access token may already be expired at that point — that's
    the whole reason to call refresh) — so they can't rely on the CSRF check folded into
    get_current_user (app/deps.py) and need their own, checked against the same row already
    looked up for the refresh-token validation itself."""
    header_value = request.headers.get(CSRF_HEADER)
    if not header_value or not secrets.compare_digest(row.csrf_token, header_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF-verifiering misslyckades.")


@router.post("/login", response_model=SessionOut)
@limiter.limit(f"{settings.rate_limit_login_per_minute}/minute")
def login(request: Request, response: Response, payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=None, action="login_failed", detail=payload.email, request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Fel e-post eller lösenord.")

    # A brand new family_id every login — never extends or reuses a prior session's
    # rotation chain. Prevents session fixation by construction: nothing about a session
    # exists, even server-side, until this exact point, and every login starts fresh.
    _, csrf_token = _issue_session(db, request, response, user, family_id=uuid.uuid4())
    record_audit(db, user_id=user.id, action="login_success", request=request)
    return _session_response(user, csrf_token)


@router.post("/refresh", response_model=SessionOut)
@limiter.limit(f"{settings.rate_limit_refresh_per_minute}/minute")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    refresh_plain = request.cookies.get(REFRESH_COOKIE)
    if refresh_plain is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ingen session att förnya.")

    token_hash = hash_refresh_token(refresh_plain)
    row = db.query(RefreshToken).filter_by(token_hash=token_hash).first()

    if row is None:
        record_audit(db, user_id=None, action="refresh_failed", detail="unknown token", request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ogiltig session.")

    _verify_csrf(request, row)

    if row.revoked_at is not None:
        # This exact token was already used once before (or explicitly revoked by a
        # logout). Presenting it again means either the legitimate client is replaying a
        # stale cookie (harmless but still denied) or an attacker captured it after the
        # real user already rotated past it (a genuine compromise signal). We can't tell
        # those apart, so we treat it as a compromise either way: kill every token in the
        # family — refresh AND their paired access tokens — so nothing from a stolen chain
        # can be used again from here, forcing full re-login.
        family_rows = db.query(RefreshToken).filter(
            RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)
        ).all()
        for family_row in family_rows:
            family_row.revoked_at = datetime.utcnow()
            db.add(family_row)
            revoke_access_token(db, family_row.access_jti)
        db.commit()
        record_audit(
            db,
            user_id=row.user_id,
            action="refresh_token_reuse_detected",
            entity_type="refresh_token_family",
            entity_id=str(row.family_id),
            request=request,
        )
        clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessionen har återkallats. Logga in igen.")

    if row.expires_at < datetime.utcnow():
        record_audit(db, user_id=row.user_id, action="refresh_failed", detail="expired", request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessionen har gått ut. Logga in igen.")

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        record_audit(db, user_id=row.user_id, action="refresh_failed", detail="inactive user", request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kontot finns inte eller är inaktiverat.")

    # Rotate: this token is now spent. A new one takes its place in the same family. Its
    # paired access token is deliberately NOT blocklisted here — a normal rotation isn't a
    # compromise, and the old access token naturally expires within minutes regardless.
    row.revoked_at = datetime.utcnow()
    db.add(row)
    db.commit()

    new_row, csrf_token = _issue_session(db, request, response, user, family_id=row.family_id)
    # Purely for audit/forensics — not used by any security check (reuse detection keys
    # off revoked_at + family_id, not this chain pointer).
    new_row.replaces_id = row.id
    db.add(new_row)
    db.commit()

    record_audit(db, user_id=user.id, action="refresh_success", request=request)
    return _session_response(user, csrf_token)


@router.post("/logout")
@limiter.limit(f"{settings.rate_limit_logout_per_minute}/minute")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    refresh_plain = request.cookies.get(REFRESH_COOKIE)
    user_id = None
    if refresh_plain is not None:
        token_hash = hash_refresh_token(refresh_plain)
        row = db.query(RefreshToken).filter_by(token_hash=token_hash).first()
        if row is not None:
            _verify_csrf(request, row)
            if row.revoked_at is None:
                row.revoked_at = datetime.utcnow()
                db.add(row)
                # Immediately invalidates the still-live access token too — a logout that
                # only revoked the refresh token would leave the current access token
                # usable for up to access_token_expire_minutes more, which isn't
                # "fullständig utloggning".
                revoke_access_token(db, row.access_jti)
                db.commit()
                user_id = row.user_id

    clear_session_cookies(response)
    record_audit(db, user_id=user_id, action="logout", request=request)
    return {"status": "logged_out"}


@router.get("/me", response_model=SessionOut)
def me(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Also returns the current CSRF value (looked up via the access token's jti, set on
    # request.state by get_current_user) so a hard page reload can repopulate the
    # frontend's in-memory copy without forcing a token refresh just to get it.
    row = db.query(RefreshToken).filter_by(access_jti=getattr(request.state, "access_jti", None)).first()
    csrf_token = row.csrf_token if row is not None else ""
    return _session_response(user, csrf_token)
