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
from app.email import send_email
from app.limiter import limiter
from app.models.audit import AuditLog
from app.models.email_verification_token import EmailVerificationToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.password_policy import validate_password
from app.schemas import EmailIn, LoginIn, RegisterIn, ResetPasswordIn, SessionOut, VerifyEmailIn
from app.security import (
    create_access_token,
    generate_csrf_token,
    generate_opaque_token,
    generate_refresh_token,
    hash_opaque_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.token_revocation import revoke_access_token, revoke_all_sessions_for_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()

# Generic, identical response bodies for anything that could otherwise leak whether an email
# address has an account — see docs/AUTH_THREAT_MODEL.md. Same string used everywhere it
# applies so there's no accidental tell in wording between call sites.
NEUTRAL_REGISTER_RESPONSE = {"detail": "Om e-postadressen inte redan används har vi skickat ett bekräftelsemail."}
NEUTRAL_FORGOT_PASSWORD_RESPONSE = {
    "detail": "Om e-postadressen finns registrerad har vi skickat instruktioner för återställning."
}
NEUTRAL_RESEND_VERIFICATION_RESPONSE = {
    "detail": "Om kontot finns och inte redan är verifierat har vi skickat ett nytt bekräftelsemail."
}

# Per-account brute-force guard, independent of (and in addition to) the per-IP rate limit
# on /login: a distributed attack spread across many IPs would sail through IP-based limiting
# alone. login_failed is recorded identically whether the account exists or not (see login()
# below), so counting by email here reveals nothing an attacker didn't already control.
FAILED_LOGIN_WINDOW_MINUTES = 15
FAILED_LOGIN_THRESHOLD = 10


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
    return SessionOut(
        id=user.id, email=user.email, role=user.role.value, email_verified=user.email_verified, csrf_token=csrf_token
    )


def _verify_csrf(request: Request, row: RefreshToken) -> None:
    """Used by /refresh and /logout, which authenticate via the refresh-token cookie rather
    than get_current_user (the access token may already be expired at that point — that's
    the whole reason to call refresh) — so they can't rely on the CSRF check folded into
    get_current_user (app/deps.py) and need their own, checked against the same row already
    looked up for the refresh-token validation itself."""
    header_value = request.headers.get(CSRF_HEADER)
    if not header_value or not secrets.compare_digest(row.csrf_token, header_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF-verifiering misslyckades.")


def _recent_failed_logins(db: Session, email: str) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=FAILED_LOGIN_WINDOW_MINUTES)
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "login_failed", AuditLog.detail == email, AuditLog.created_at >= cutoff)
        .count()
    )


def _issue_and_send_verification(db: Session, user: User, request: Request) -> None:
    # Invalidate any previous outstanding link before issuing a new one — only one valid
    # verification link should ever exist for an account at a time.
    db.query(EmailVerificationToken).filter_by(user_id=user.id, used_at=None).update({"used_at": datetime.utcnow()})
    token_plain = generate_opaque_token()
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_opaque_token(token_plain),
            expires_at=datetime.utcnow() + timedelta(hours=settings.email_verification_token_expire_hours),
        )
    )
    db.commit()
    link = f"{settings.public_app_url}/verify-email?token={token_plain}"
    send_email(
        user.email,
        "Bekräfta din e-postadress – MainAI",
        "Klicka på länken nedan för att bekräfta ditt konto:\n\n"
        f"{link}\n\n"
        f"Länken är giltig i {settings.email_verification_token_expire_hours} timmar och kan bara användas en gång.\n\n"
        "Om du inte skapade det här kontot kan du bortse från det här mejlet.",
    )


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(f"{settings.rate_limit_register_per_minute}/minute")
def register(request: Request, payload: RegisterIn, db: Session = Depends(get_db)):
    # Honeypot: a real user's browser never populates this hidden field. Return the exact
    # same response a genuine registration gets, so the bot can't distinguish "caught" from
    # "processed" and adjust — but skip all DB writes and the email send entirely.
    if payload.website:
        record_audit(db, user_id=None, action="register_bot_suspected", request=request)
        return NEUTRAL_REGISTER_RESPONSE

    try:
        validate_password(payload.password, email=payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = db.query(User).filter_by(email=payload.email).first()
    if existing is not None:
        if not existing.email_verified:
            # Legitimate case: someone lost or never received the first verification email.
            # Safe to resend — the response is identical either way, so this doesn't leak
            # anything beyond what a would-be attacker could already tell from the response.
            _issue_and_send_verification(db, existing, request)
            record_audit(db, user_id=existing.id, action="register_resent_verification", request=request)
        else:
            record_audit(db, user_id=existing.id, action="register_duplicate_attempt", request=request)
        return NEUTRAL_REGISTER_RESPONSE

    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    _issue_and_send_verification(db, user, request)
    record_audit(db, user_id=user.id, action="register_success", request=request)
    return NEUTRAL_REGISTER_RESPONSE


@router.post("/verify-email")
@limiter.limit(f"{settings.rate_limit_verify_email_per_minute}/minute")
def verify_email(request: Request, payload: VerifyEmailIn, db: Session = Depends(get_db)):
    row = db.query(EmailVerificationToken).filter_by(token_hash=hash_opaque_token(payload.token)).first()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        record_audit(db, user_id=row.user_id if row else None, action="email_verify_failed", request=request)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Länken är ogiltig eller har gått ut. Begär en ny."
        )
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Länken är ogiltig eller har gått ut. Begär en ny."
        )

    row.used_at = datetime.utcnow()
    user.email_verified = True
    user.email_verified_at = datetime.utcnow()
    db.add_all([row, user])
    db.commit()
    record_audit(db, user_id=user.id, action="email_verified", request=request)
    return {"status": "verified"}


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(f"{settings.rate_limit_register_per_minute}/minute")
def resend_verification(request: Request, payload: EmailIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    if user is None or user.email_verified:
        return NEUTRAL_RESEND_VERIFICATION_RESPONSE
    _issue_and_send_verification(db, user, request)
    record_audit(db, user_id=user.id, action="resend_verification", request=request)
    return NEUTRAL_RESEND_VERIFICATION_RESPONSE


@router.post("/login", response_model=SessionOut)
@limiter.limit(f"{settings.rate_limit_login_per_minute}/minute")
def login(request: Request, response: Response, payload: LoginIn, db: Session = Depends(get_db)):
    if _recent_failed_logins(db, payload.email) >= FAILED_LOGIN_THRESHOLD:
        record_audit(db, user_id=None, action="login_blocked_too_many_attempts", detail=payload.email, request=request)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="För många misslyckade inloggningsförsök för det här kontot. Försök igen om en stund.",
        )

    user = db.query(User).filter_by(email=payload.email).first()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=None, action="login_failed", detail=payload.email, request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Fel e-post eller lösenord.")

    if not user.email_verified:
        # Deliberately NOT counted as a login_failed attempt (it's not a credential-guessing
        # signal — the password was correct) and no session is issued: full app access stays
        # blocked until the account is verified, not just cosmetically restricted post-login.
        record_audit(db, user_id=user.id, action="login_blocked_unverified", request=request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kontot är inte verifierat än. Kolla din e-post eller begär ett nytt bekräftelsemail.",
        )

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


@router.post("/logout-all")
@limiter.limit(f"{settings.rate_limit_logout_per_minute}/minute")
def logout_all(
    request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Ends every session for this account, on every device — not just the one making the
    request. get_current_user already enforces CSRF for this (POST is a mutating method)."""
    revoke_all_sessions_for_user(db, user.id)
    clear_session_cookies(response)
    record_audit(db, user_id=user.id, action="logout_all_devices", request=request)
    return {"status": "logged_out_all"}


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(f"{settings.rate_limit_forgot_password_per_minute}/minute")
def forgot_password(request: Request, payload: EmailIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    if user is None or not user.is_active:
        record_audit(db, user_id=None, action="forgot_password_unknown_email", request=request)
        return NEUTRAL_FORGOT_PASSWORD_RESPONSE

    db.query(PasswordResetToken).filter_by(user_id=user.id, used_at=None).update({"used_at": datetime.utcnow()})
    token_plain = generate_opaque_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_opaque_token(token_plain),
            expires_at=datetime.utcnow() + timedelta(hours=settings.password_reset_token_expire_hours),
        )
    )
    db.commit()
    link = f"{settings.public_app_url}/reset-password?token={token_plain}"
    send_email(
        user.email,
        "Återställ ditt lösenord – MainAI",
        "Klicka på länken nedan för att välja ett nytt lösenord:\n\n"
        f"{link}\n\n"
        f"Länken är giltig i {settings.password_reset_token_expire_hours} timme(-ar) och kan bara användas en gång.\n\n"
        "Om du inte begärde detta kan du bortse från mejlet — ditt lösenord ändras inte "
        "förrän du klickar länken och väljer ett nytt.",
    )
    record_audit(db, user_id=user.id, action="forgot_password_requested", request=request)
    return NEUTRAL_FORGOT_PASSWORD_RESPONSE


@router.post("/reset-password")
@limiter.limit(f"{settings.rate_limit_reset_password_per_minute}/minute")
def reset_password(request: Request, response: Response, payload: ResetPasswordIn, db: Session = Depends(get_db)):
    row = db.query(PasswordResetToken).filter_by(token_hash=hash_opaque_token(payload.token)).first()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        record_audit(db, user_id=row.user_id if row else None, action="reset_password_invalid_token", request=request)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Länken är ogiltig eller har gått ut. Begär en ny återställning.",
        )
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Länken är ogiltig eller har gått ut. Begär en ny återställning.",
        )

    try:
        validate_password(payload.new_password, email=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    row.used_at = datetime.utcnow()
    user.password_hash = hash_password(payload.new_password)
    db.add_all([row, user])
    db.commit()

    # A password reset is exactly the situation where old sessions must not survive — if the
    # password was compromised, whoever had it may also be holding a live session cookie.
    revoke_all_sessions_for_user(db, user.id)
    clear_session_cookies(response)
    record_audit(db, user_id=user.id, action="password_reset_success", request=request)
    return {"status": "password_reset"}


@router.get("/me", response_model=SessionOut)
def me(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Also returns the current CSRF value (looked up via the access token's jti, set on
    # request.state by get_current_user) so a hard page reload can repopulate the
    # frontend's in-memory copy without forcing a token refresh just to get it.
    row = db.query(RefreshToken).filter_by(access_jti=getattr(request.state, "access_jti", None)).first()
    csrf_token = row.csrf_token if row is not None else ""
    return _session_response(user, csrf_token)
