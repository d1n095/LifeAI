import uuid
from datetime import datetime, timedelta

from app.cleanup import run_token_cleanup
from app.config import get_settings
from app.models.email_verification_token import EmailVerificationToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.revoked_access_token import RevokedAccessToken
from app.security import hash_opaque_token, hash_refresh_token


def test_purges_old_expired_refresh_tokens_but_keeps_recent_ones(db_session, make_verified_user):
    user, _ = make_verified_user()
    settings = get_settings()
    retention = settings.token_cleanup_retention_days

    old = RefreshToken(
        user_id=user.id,
        family_id=uuid.uuid4(),
        token_hash=hash_refresh_token("old-token"),
        csrf_token="csrf",
        expires_at=datetime.utcnow() - timedelta(days=retention + 1),
    )
    recent = RefreshToken(
        user_id=user.id,
        family_id=uuid.uuid4(),
        token_hash=hash_refresh_token("recent-token"),
        csrf_token="csrf",
        expires_at=datetime.utcnow() - timedelta(days=retention - 1),
    )
    db_session.add_all([old, recent])
    db_session.commit()

    counts = run_token_cleanup(db_session)

    assert counts["refresh_tokens"] == 1
    remaining = db_session.query(RefreshToken).all()
    assert len(remaining) == 1
    assert remaining[0].id == recent.id


def test_purges_refresh_tokens_revoked_past_retention_even_if_not_yet_naturally_expired(
    db_session, make_verified_user
):
    """A refresh token revoked (e.g. reuse-detected) long ago but with a far-future natural
    expiry must still be purged after the retention window — revocation time governs, not
    just natural expiry."""
    user, _ = make_verified_user()
    settings = get_settings()
    retention = settings.token_cleanup_retention_days

    row = RefreshToken(
        user_id=user.id,
        family_id=uuid.uuid4(),
        token_hash=hash_refresh_token("revoked-long-ago"),
        csrf_token="csrf",
        expires_at=datetime.utcnow() + timedelta(days=100),
        revoked_at=datetime.utcnow() - timedelta(days=retention + 1),
    )
    db_session.add(row)
    db_session.commit()

    counts = run_token_cleanup(db_session)
    assert counts["refresh_tokens"] == 1
    assert db_session.query(RefreshToken).count() == 0


def test_purges_revoked_access_tokens_past_natural_expiry(db_session):
    expired = RevokedAccessToken(jti=str(uuid.uuid4()), expires_at=datetime.utcnow() - timedelta(minutes=1))
    still_valid = RevokedAccessToken(jti=str(uuid.uuid4()), expires_at=datetime.utcnow() + timedelta(minutes=10))
    db_session.add_all([expired, still_valid])
    db_session.commit()

    counts = run_token_cleanup(db_session)

    assert counts["revoked_access_tokens"] == 1
    remaining = db_session.query(RevokedAccessToken).all()
    assert len(remaining) == 1
    assert remaining[0].jti == still_valid.jti


def test_purges_old_used_verification_and_reset_tokens(db_session, make_verified_user):
    user, _ = make_verified_user()
    settings = get_settings()
    retention = settings.token_cleanup_retention_days

    old_verification = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_opaque_token("v1"),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        used_at=datetime.utcnow() - timedelta(days=retention + 1),
    )
    recent_verification = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_opaque_token("v2"),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        used_at=datetime.utcnow(),
    )
    old_reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_opaque_token("r1"),
        expires_at=datetime.utcnow() - timedelta(days=retention + 1),
    )
    db_session.add_all([old_verification, recent_verification, old_reset])
    db_session.commit()

    counts = run_token_cleanup(db_session)

    assert counts["email_verification_tokens"] == 1
    assert counts["password_reset_tokens"] == 1
    assert db_session.query(EmailVerificationToken).count() == 1
    assert db_session.query(PasswordResetToken).count() == 0


def test_is_idempotent(db_session):
    first = run_token_cleanup(db_session)
    second = run_token_cleanup(db_session)
    assert first == {"refresh_tokens": 0, "revoked_access_tokens": 0, "email_verification_tokens": 0, "password_reset_tokens": 0}
    assert second == first


def test_advisory_lock_prevents_concurrent_runs(db_session):
    """Simulates a second, already-running cleanup pass by holding the same advisory lock
    on a separate connection — the fixture's run should see the lock unavailable and return
    None instead of doing (or racing on) the work."""
    from sqlalchemy import text

    from app.cleanup import _CLEANUP_LOCK_KEY
    from app.db import SessionLocal

    other_connection = SessionLocal()
    try:
        other_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _CLEANUP_LOCK_KEY})
        result = run_token_cleanup(db_session)
        assert result is None
    finally:
        other_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _CLEANUP_LOCK_KEY})
        other_connection.commit()
        other_connection.close()
