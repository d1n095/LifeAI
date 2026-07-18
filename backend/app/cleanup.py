import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.models.email_verification_token import EmailVerificationToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.revoked_access_token import RevokedAccessToken

logger = logging.getLogger("mainai.cleanup")
settings = get_settings()

# Arbitrary fixed key for a Postgres advisory lock — any consistent 64-bit int works, it
# just needs to be the same across every process/instance so they contend on the same lock.
_CLEANUP_LOCK_KEY = 875_190_442


def run_token_cleanup(db: Session) -> dict[str, int] | None:
    """Purges auth-token rows past their security-relevant retention window:
    refresh_tokens, revoked_access_tokens, email_verification_tokens and
    password_reset_tokens.

    Idempotent: pure delete-by-timestamp, so re-running it (or two overlapping runs, e.g.
    one per backend replica on the same schedule) never double-deletes or errors — a
    Postgres advisory lock additionally ensures only ONE concurrent caller does the actual
    work; the rest return None immediately instead of blocking or racing.

    Retention (settings.token_cleanup_retention_days, default 30) exists for
    security-incident forensics — e.g. investigating a refresh-token-reuse event after the
    fact needs the revoked row's IP/user-agent/timestamps to still exist for a while, not to
    vanish the moment the token is spent. Past that window nothing about a dead auth token
    is useful, so the row is purged outright, not just its sensitive fields.

    revoked_access_tokens is the one exception with no retention delay: it holds no
    forensic metadata at all (just a jti and its natural expiry — see
    app/models/revoked_access_token.py), so it's purged as soon as the underlying access
    token would have expired anyway, same as the opportunistic pruning already done inline
    in app/token_revocation.py.
    """
    # The advisory lock is acquired and released on a single, dedicated connection held for
    # this entire function — deliberately NOT via db.execute(). Session-level advisory locks
    # (pg_advisory_lock) belong to a specific physical connection, but a SQLAlchemy Session
    # releases its connection back to the pool on every commit(); acquiring the lock, then
    # calling db.commit() for the deletes below, then trying to unlock via db.execute() again
    # can silently check out a DIFFERENT pooled connection for that second call — which
    # "succeeds" (no error) without actually releasing the lock the first connection is still
    # holding, leaking it for the lifetime of that pooled connection. Keeping our own
    # connection pinned start-to-finish makes acquire and release provably the same session.
    lock_conn = engine.connect()
    try:
        acquired = lock_conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _CLEANUP_LOCK_KEY}).scalar()
        lock_conn.commit()
        if not acquired:
            logger.info("Städjobb hoppas över — ett annat pass körs redan (advisory lock upptagen).")
            return None

        try:
            cutoff = datetime.utcnow() - timedelta(days=settings.token_cleanup_retention_days)
            now = datetime.utcnow()

            counts = {
                "refresh_tokens": db.query(RefreshToken)
                .filter(
                    (RefreshToken.expires_at < cutoff)
                    | ((RefreshToken.revoked_at.isnot(None)) & (RefreshToken.revoked_at < cutoff))
                )
                .delete(synchronize_session=False),
                "revoked_access_tokens": db.query(RevokedAccessToken)
                .filter(RevokedAccessToken.expires_at < now)
                .delete(synchronize_session=False),
                "email_verification_tokens": db.query(EmailVerificationToken)
                .filter(
                    (EmailVerificationToken.expires_at < cutoff)
                    | ((EmailVerificationToken.used_at.isnot(None)) & (EmailVerificationToken.used_at < cutoff))
                )
                .delete(synchronize_session=False),
                "password_reset_tokens": db.query(PasswordResetToken)
                .filter(
                    (PasswordResetToken.expires_at < cutoff)
                    | ((PasswordResetToken.used_at.isnot(None)) & (PasswordResetToken.used_at < cutoff))
                )
                .delete(synchronize_session=False),
            }
            db.commit()
            logger.info("Städjobb klart: %s", counts)
            return counts
        except Exception:
            db.rollback()
            raise
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _CLEANUP_LOCK_KEY})
            lock_conn.commit()
    finally:
        lock_conn.close()
