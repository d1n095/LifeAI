"""Passkeys / WebAuthn: webauthn_credentials (RLS owner-scoped) + webauthn_challenges.

Revision id is DELIBERATELY NON-NUMERIC (`webauthn_passkeys_01`) — the numeric migration space
0069-0099 is a collision storm across lineages (0069 alone exists in 5 variants; the #312 line
runs 0069_workforce_authority_epoch..0089), so any numeric slot would collide. See
docs/PASSKEYS_WEBAUTHN_DESIGN.md §3.

down_revision is pinned to THIS branch's alembic head (`0068`). The unique id removes the ID
collision but NOT the Alembic ancestry/merge-head problem: at integration into a lineage that
also descends from 0068, there will be two heads — this migration's down_revision must then be
re-pointed to that lineage's real head, or reconciled with an `alembic merge`. That is tracked
explicitly in docs/BRANCH_REGISTRY.md and must be resolved before integration.

RLS: webauthn_credentials is owner-scoped (same pattern as mainai_jobs / migration 0068) AND is
also registered in app/rls.py (RLS_STATEMENTS + POLICY_DEFINITIONS) so apply_rls repairs it and
the RLS policy-registry test stays consistent. webauthn_challenges is intentionally NOT RLS: the
authentication ceremony runs BEFORE login, so there is no app.current_user_id to scope by; the
row holds only a random challenge keyed by an unguessable handle, is single-use and short-lived,
and carries no sensitive data.
"""

from alembic import op

revision = "webauthn_passkeys_01"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webauthn_credentials (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            credential_id bytea NOT NULL,
            public_key bytea NOT NULL,
            sign_count bigint NOT NULL DEFAULT 0,
            transports varchar(32)[] NOT NULL DEFAULT '{}',
            aaguid uuid,
            backup_eligible boolean NOT NULL DEFAULT false,
            backup_state boolean NOT NULL DEFAULT false,
            label varchar(100) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz,
            CONSTRAINT uq_webauthn_credentials_credential_id UNIQUE (credential_id),
            CONSTRAINT ck_webauthn_credentials_label CHECK (length(btrim(label)) > 0)
        );
        CREATE INDEX ix_webauthn_credentials_owner_id ON webauthn_credentials USING btree (owner_id);

        ALTER TABLE webauthn_credentials ENABLE ROW LEVEL SECURITY;
        ALTER TABLE webauthn_credentials FORCE ROW LEVEL SECURITY;
        CREATE POLICY webauthn_credentials_isolation ON webauthn_credentials
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE TABLE webauthn_challenges (
            handle varchar(64) PRIMARY KEY,
            challenge bytea NOT NULL,
            ceremony varchar(16) NOT NULL,
            user_id uuid REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            CONSTRAINT ck_webauthn_challenges_ceremony CHECK (ceremony IN ('register', 'authenticate'))
        );
        CREATE INDEX ix_webauthn_challenges_expires_at ON webauthn_challenges USING btree (expires_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS webauthn_challenges;
        DROP POLICY IF EXISTS webauthn_credentials_isolation ON webauthn_credentials;
        DROP TABLE IF EXISTS webauthn_credentials;
        """
    )
