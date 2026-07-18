"""Cookie-based session milestone: refresh_tokens (rotation + reuse detection) and
revoked_access_tokens (immediate access-token revocation on logout). See
docs/AUTH_THREAT_MODEL.md.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE refresh_tokens (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            family_id uuid NOT NULL,
            token_hash character varying(64) NOT NULL,
            access_jti character varying(36),
            csrf_token character varying(64) NOT NULL,
            replaces_id uuid REFERENCES refresh_tokens(id),
            created_at timestamp without time zone NOT NULL,
            expires_at timestamp without time zone NOT NULL,
            revoked_at timestamp without time zone,
            user_agent character varying(512),
            ip_address character varying(64)
        );
        CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens USING btree (user_id);
        CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens USING btree (family_id);
        CREATE UNIQUE INDEX ix_refresh_tokens_token_hash ON refresh_tokens USING btree (token_hash);
    """)

    op.execute("""
        CREATE TABLE revoked_access_tokens (
            jti character varying(36) NOT NULL PRIMARY KEY,
            expires_at timestamp without time zone NOT NULL
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE revoked_access_tokens;
        DROP TABLE refresh_tokens;
    """)
