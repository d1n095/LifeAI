import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings

settings = get_settings()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, str]:
    """Returns (token, jti). The jti (JWT ID) is what makes instant logout possible despite
    the access token being a stateless, self-contained JWT: logout records this specific
    jti in a short-lived blocklist (see app/routers/auth.py, models/revoked_access_token.py)
    instead of only revoking the refresh token and waiting out the access token's natural
    expiry."""
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "role": role, "exp": expire, "jti": jti}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), jti


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


def generate_refresh_token() -> str:
    """High-entropy opaque token — deliberately not a JWT. A random 48-byte token is
    trivially revocable (delete/flag the DB row); a JWT refresh token would still be
    cryptographically "valid" after revocation unless we kept a blocklist anyway, which
    would mean maintaining the same DB state a plain opaque token needs regardless."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 is appropriate here (unlike for passwords): the input is already
    high-entropy random, not a human-guessable secret, so there's no offline
    brute-force risk that bcrypt-style deliberate slowness would defend against."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
