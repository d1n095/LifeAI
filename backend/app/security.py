import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

from app.config import get_settings

settings = get_settings()


def utcnow_seconds() -> datetime:
    """Whole-second-precision UTC "now" — use this (not datetime.utcnow()) for anything
    later compared against a JWT's iat claim, e.g. User.sessions_valid_after. JWT NumericDate
    (RFC 7519 §2) has whole-second precision, so PyJWT truncates iat's sub-second part away
    on encode. Comparing an iat against a microsecond-precision timestamp is a real bug, not
    a theoretical one: a token issued in the same wall-clock second as, say,
    User.sessions_valid_after being set (entirely possible — a freshly bootstrapped account
    logging in immediately, or a password reset immediately followed by re-login) can have
    its truncated iat land BEFORE that same-second microsecond-precision value purely by
    truncation, and be incorrectly treated as pre-dating a revocation it actually came after.

    Use for *revocation* timestamps (password reset, "log out everywhere" —
    app/token_revocation.py): app/deps.py's iat check is <=, deliberately failing closed on
    a same-second tie (see its comment), so this value should be the genuine, un-adjusted
    "now" of the revocation event. For *baseline* timestamps set once at account creation
    (bootstrap admin, a fresh registration's default — never a revocation), use
    utcnow_seconds_baseline() instead, which backdates by one second so a brand-new
    account's own very first login can never collide with its own creation-time baseline
    under that same <= rule."""
    return datetime.utcnow().replace(microsecond=0)


def utcnow_seconds_baseline() -> datetime:
    """See utcnow_seconds() — the same whole-second truncation, backdated by one second so a
    freshly created account's own creation-time sessions_valid_after can never tie with (and
    thus never fail-closed-reject) that same account's first real login, even in the
    same wall-clock second (routine in fast automated tests; astronomically unlikely but not
    impossible for a real user)."""
    return utcnow_seconds() - timedelta(seconds=1)


# Argon2id (argon2-cffi's default Type.ID) — winner of the Password Hashing Competition and
# the current OWASP-recommended choice for new applications; deliberately slow and memory-hard
# to resist GPU/ASIC offline cracking in a way bcrypt's fixed small memory footprint no longer
# does as well. Default cost parameters (time_cost=3, memory_cost=64 MiB, parallelism=4) are
# OWASP's own baseline recommendation, not tuned down for convenience.
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, str]:
    """Returns (token, jti). The jti (JWT ID) is what makes instant logout possible despite
    the access token being a stateless, self-contained JWT: logout records this specific
    jti in a short-lived blocklist (see app/routers/auth.py, models/revoked_access_token.py)
    instead of only revoking the refresh token and waiting out the access token's natural
    expiry.

    The iat (issued-at) claim is what makes bulk revocation possible without enumerating
    every outstanding token: password reset and "log out everywhere" both just bump
    User.sessions_valid_after, and app/deps.py rejects any access token issued before that
    timestamp — one column write invalidates every session at once, old tokens included."""
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": expire, "jti": jti}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), jti


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


def generate_refresh_token() -> str:
    """High-entropy opaque token — deliberately not a JWT. A random 48-byte token is
    trivially revocable (delete/flag the DB row); a JWT refresh token would still be
    cryptographically "valid" after revocation unless we kept a blocklist anyway, which
    would mean maintaining the same DB state a plain opaque token needs regardless."""
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    """SHA-256 hex digest, shared by refresh tokens, email-verification tokens and
    password-reset tokens: all are high-entropy random values, not human-guessable
    secrets, so there's no offline brute-force risk that Argon2id-style deliberate
    slowness would defend against — a fast hash is the correct (and faster) choice here."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_refresh_token(token: str) -> str:
    return hash_opaque_token(token)


def generate_opaque_token(nbytes: int = 32) -> str:
    """Used for email-verification and password-reset links — 256 bits of entropy by
    default, infeasible to guess even with the token's natural exposure in an email link
    (see docs/AUTH_THREAT_MODEL.md for why that's an acceptable, unavoidable trade-off for
    this specific class of token, unlike the session cookies)."""
    return secrets.token_urlsafe(nbytes)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
