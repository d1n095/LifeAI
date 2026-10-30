"""Passkeys / WebAuthn HTTP endpoints — `/api/auth/webauthn/*`.

This is the DB/session glue around the authoritative verification core (app/webauthn_service.py).
Design notes (see docs/PASSKEYS_WEBAUTHN_DESIGN.md):

- Passkey login issues the SAME session as password login — it reuses app/routers/auth.py's
  `_issue_session` / `_session_response` (same HttpOnly cookies, same CSRF value, a fresh
  family_id). Passkeys are an additional credential on the existing account, not a parallel auth.
- RLS: webauthn_credentials is owner-scoped. Authentication precedes login, so there is no
  app.current_user_id yet; we set it from the challenge's stored user_id (a *claim* of identity,
  exactly like typing a username) before looking up the credential. That keeps the lookup
  RLS-scoped WITHOUT any SECURITY DEFINER bypass — an attacker who claims another user still
  cannot authenticate, because the cryptographic assertion must be signed by that user's
  authenticator (verified in webauthn_service.verify_authentication).
- Challenges are single-use + time-boxed (webauthn_challenges, non-RLS).
- Sensitive op (revoking a passkey) requires a fresh step-up: the current password, re-verified
  at the moment of deletion.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models.user import User
from app.models.webauthn import WebAuthnChallenge, WebAuthnCredential
from app.routers.auth import _issue_session, _session_response
from app.schemas import SessionOut
from app.security import verify_password
from app import webauthn_service as wa

import uuid

router = APIRouter(prefix="/api/auth/webauthn", tags=["webauthn"])
settings = get_settings()

CHALLENGE_TTL = timedelta(minutes=5)
# Biometric by policy for the real deployment (settings default True); tests that drive a
# software authenticator with no UV bit set this False via settings/monkeypatch.
REQUIRE_UV = settings.webauthn_require_user_verification


def _rp_origins() -> list[str]:
    return [o.strip() for o in settings.webauthn_rp_origin.split(",") if o.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_handle() -> str:
    return secrets.token_urlsafe(32)


def _set_rls_user(db: Session, user_id: uuid.UUID) -> None:
    # Scope this transaction's webauthn_credentials access to one owner (RLS). Used on the
    # pre-login authentication path, where get_current_user has not set the GUC.
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _store_challenge(db: Session, handle: str, challenge: bytes, ceremony: str, user_id):
    db.add(
        WebAuthnChallenge(
            handle=handle,
            challenge=challenge,
            ceremony=ceremony,
            user_id=user_id,
            expires_at=_now() + CHALLENGE_TTL,
        )
    )
    db.commit()


def _consume_challenge(db: Session, handle: str, ceremony: str) -> WebAuthnChallenge:
    row = db.query(WebAuthnChallenge).filter_by(handle=handle).first()
    if (
        row is None
        or row.ceremony != ceremony
        or row.consumed_at is not None
        or row.expires_at < _now()
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ogiltig eller utgången WebAuthn-begäran.")
    row.consumed_at = _now()
    db.commit()
    return row


# ----------------------------------------------------------------------------- registration

class RegisterBeginOut(BaseModel):
    handle: str
    options: dict


class RegisterCompleteIn(BaseModel):
    handle: str
    credential: dict
    label: str = Field(min_length=1, max_length=100)


@router.post("/register/begin", response_model=RegisterBeginOut)
def register_begin(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = db.query(WebAuthnCredential).filter_by(owner_id=user.id).all()
    options_json, challenge = wa.registration_options_json(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(user.id).encode(),
        user_name=user.email,
        user_display_name=user.email,
        exclude_credential_ids=[c.credential_id for c in existing],
        require_user_verification=REQUIRE_UV,
    )
    handle = _new_handle()
    _store_challenge(db, handle, challenge, "register", user.id)
    return RegisterBeginOut(handle=handle, options=json.loads(options_json))


@router.post("/register/complete", response_model=SessionOut)
def register_complete(
    request: Request,
    payload: RegisterCompleteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    chal = _consume_challenge(db, payload.handle, "register")
    if chal.user_id != user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WebAuthn-begäran hör inte till den här sessionen.")
    try:
        reg = wa.verify_registration(
            credential=json.dumps(payload.credential),
            expected_challenge=bytes(chal.challenge),
            rp_id=settings.webauthn_rp_id,
            expected_origin=_rp_origins(),
            require_user_verification=REQUIRE_UV,
        )
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Kunde inte verifiera passkey-registreringen.")

    db.add(
        WebAuthnCredential(
            owner_id=user.id,
            credential_id=reg.credential_id,
            public_key=reg.public_key,
            sign_count=reg.sign_count,
            aaguid=uuid.UUID(reg.aaguid) if reg.aaguid else None,
            label=payload.label.strip(),
        )
    )
    db.commit()
    # Return the current session identity (the caller is already logged in).
    return _session_response(user, request.headers.get("x-csrf-token", ""))


# --------------------------------------------------------------------------- authentication

class AuthenticateBeginIn(BaseModel):
    email: str


class AuthenticateBeginOut(BaseModel):
    handle: str
    options: dict


class AuthenticateCompleteIn(BaseModel):
    handle: str
    credential: dict


@router.post("/authenticate/begin", response_model=AuthenticateBeginOut)
def authenticate_begin(payload: AuthenticateBeginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    allow_ids: list[bytes] = []
    user_id = None
    if user is not None:
        user_id = user.id
        _set_rls_user(db, user.id)
        allow_ids = [c.credential_id for c in db.query(WebAuthnCredential).filter_by(owner_id=user.id).all()]
    options_json, challenge = wa.authentication_options_json(
        rp_id=settings.webauthn_rp_id,
        allow_credential_ids=allow_ids,
        require_user_verification=REQUIRE_UV,
    )
    handle = _new_handle()
    # If the email is unknown we still return a well-formed challenge (no user enumeration);
    # completion will simply fail to find a credential.
    _store_challenge(db, handle, challenge, "authenticate", user_id)
    return AuthenticateBeginOut(handle=handle, options=json.loads(options_json))


@router.post("/authenticate/complete", response_model=SessionOut)
def authenticate_complete(
    request: Request,
    response: Response,
    payload: AuthenticateCompleteIn,
    db: Session = Depends(get_db),
):
    chal = _consume_challenge(db, payload.handle, "authenticate")
    if chal.user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ingen passkey matchade.")

    user = db.query(User).filter_by(id=chal.user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ingen passkey matchade.")

    # Scope to the claimed owner so the credential lookup is RLS-enforced.
    _set_rls_user(db, user.id)

    # The raw credential id is base64url in the client JSON; match against stored bytea.
    raw_id = payload.credential.get("rawId") or payload.credential.get("id")
    if not isinstance(raw_id, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ogiltig passkey-uppgift.")
    import base64

    pad = "=" * (-len(raw_id) % 4)
    try:
        cred_id_bytes = base64.urlsafe_b64decode(raw_id + pad)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ogiltig passkey-uppgift.")

    cred = (
        db.query(WebAuthnCredential)
        .filter_by(owner_id=user.id, credential_id=cred_id_bytes)
        .first()
    )
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ingen passkey matchade.")

    try:
        result = wa.verify_authentication(
            credential=json.dumps(payload.credential),
            expected_challenge=bytes(chal.challenge),
            rp_id=settings.webauthn_rp_id,
            expected_origin=_rp_origins(),
            public_key=bytes(cred.public_key),
            current_sign_count=cred.sign_count,
            require_user_verification=REQUIRE_UV,
        )
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Passkey-autentisering misslyckades.")

    cred.sign_count = result.new_sign_count
    cred.last_used_at = _now()
    db.commit()

    # Issue the normal session (fresh family) — identical to password login.
    _, csrf_token = _issue_session(db, request, response, user, family_id=uuid.uuid4())
    return _session_response(user, csrf_token)


# ------------------------------------------------------------------------------- management

class CredentialOut(BaseModel):
    id: str
    label: str
    transports: list[str]
    created_at: datetime
    last_used_at: datetime | None


class DeleteCredentialIn(BaseModel):
    password: str


@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(WebAuthnCredential)
        .filter_by(owner_id=user.id)
        .order_by(WebAuthnCredential.created_at.desc())
        .all()
    )
    return [
        CredentialOut(
            id=str(r.id),
            label=r.label,
            transports=list(r.transports or []),
            created_at=r.created_at,
            last_used_at=r.last_used_at,
        )
        for r in rows
    ]


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    credential_id: str,
    payload: DeleteCredentialIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Fresh step-up: re-verify the current password at the moment of this sensitive op.
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fel lösenord. Ingen passkey togs bort.")
    try:
        cid = uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Passkey hittades inte.")
    cred = db.query(WebAuthnCredential).filter_by(id=cid, owner_id=user.id).first()
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Passkey hittades inte.")
    db.delete(cred)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
