"""Account lifecycle milestone: self-service registration + email verification +
password reset + bulk session revocation. See docs/AUTH_THREAT_MODEL.md ("Tillägg
2026-07-18: fullständigt kontoflöde").

Backfill notes for upgrading a database that already has users (e.g. the bootstrap admin,
or any account created before this migration): those accounts never went through the new
verification flow and must not be retroactively locked out or logged out by this migration
alone —
  - email_verified backfills to true (grandfathered — they were already trusted).
  - sessions_valid_after backfills to each user's created_at, not now(): using now() would
    immediately invalidate every currently active session the moment this migration runs,
    turning a routine deploy into a surprise mass logout. created_at is always earlier than
    any real session's issued-at time, so existing sessions keep working until they
    naturally expire or are revoked some other way.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users ADD COLUMN email_verified boolean NOT NULL DEFAULT true;
        ALTER TABLE users ADD COLUMN email_verified_at timestamp without time zone;
        ALTER TABLE users ADD COLUMN sessions_valid_after timestamp without time zone;

        UPDATE users SET email_verified_at = created_at WHERE email_verified = true;
        UPDATE users SET sessions_valid_after = created_at;

        ALTER TABLE users ALTER COLUMN sessions_valid_after SET NOT NULL;
        -- Drop the backfill-only defaults: the application always sends an explicit value
        -- on INSERT (see app/models/user.py, app/bootstrap.py), so a stale DB-level
        -- default serves no purpose going forward and would only mask a future bug where
        -- the application forgets to set one.
        ALTER TABLE users ALTER COLUMN email_verified DROP DEFAULT;
    """)

    op.execute("""
        CREATE TABLE email_verification_tokens (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            token_hash character varying(64) NOT NULL,
            created_at timestamp without time zone NOT NULL,
            expires_at timestamp without time zone NOT NULL,
            used_at timestamp without time zone
        );
        CREATE INDEX ix_email_verification_tokens_user_id ON email_verification_tokens USING btree (user_id);
        CREATE UNIQUE INDEX ix_email_verification_tokens_token_hash ON email_verification_tokens USING btree (token_hash);
    """)

    op.execute("""
        CREATE TABLE password_reset_tokens (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            token_hash character varying(64) NOT NULL,
            created_at timestamp without time zone NOT NULL,
            expires_at timestamp without time zone NOT NULL,
            used_at timestamp without time zone
        );
        CREATE INDEX ix_password_reset_tokens_user_id ON password_reset_tokens USING btree (user_id);
        CREATE UNIQUE INDEX ix_password_reset_tokens_token_hash ON password_reset_tokens USING btree (token_hash);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE password_reset_tokens;
        DROP TABLE email_verification_tokens;

        ALTER TABLE users DROP COLUMN sessions_valid_after;
        ALTER TABLE users DROP COLUMN email_verified_at;
        ALTER TABLE users DROP COLUMN email_verified;
    """)
