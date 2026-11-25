"""V4: owner-scoped runtime incident history (migration 0092).

One grouped row per stable (error_code, component, summary) fingerprint. Existing provider,
worker and sandbox failure paths record structured, already-safe facts here; prompts, replies,
documents and credentials never enter the table. RLS shape matches chat_request_claims.
"""

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE runtime_incidents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            fingerprint varchar(64) NOT NULL,
            error_code varchar(64) NOT NULL,
            component varchar(128) NOT NULL,
            severity varchar(16) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'open',
            summary varchar(512) NOT NULL,
            correlation_id varchar(128),
            app_version varchar(64),
            occurrence_count integer NOT NULL DEFAULT 1,
            first_seen_at timestamp NOT NULL DEFAULT now(),
            last_seen_at timestamp NOT NULL DEFAULT now(),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            analysis jsonb NOT NULL DEFAULT '{}'::jsonb,
            recovery_options jsonb NOT NULL DEFAULT '[]'::jsonb,
            acknowledged_at timestamp,
            resolved_at timestamp,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_runtime_incidents_owner_fingerprint UNIQUE (owner_id, fingerprint),
            CONSTRAINT ck_runtime_incidents_severity CHECK (severity IN ('low','medium','high','critical')),
            CONSTRAINT ck_runtime_incidents_status CHECK (status IN ('open','acknowledged','resolved')),
            CONSTRAINT ck_runtime_incidents_count CHECK (occurrence_count >= 1),
            CONSTRAINT ck_runtime_incidents_details CHECK (jsonb_typeof(details) = 'object'),
            CONSTRAINT ck_runtime_incidents_analysis CHECK (jsonb_typeof(analysis) = 'object'),
            CONSTRAINT ck_runtime_incidents_options CHECK (jsonb_typeof(recovery_options) = 'array')
        );
        CREATE INDEX ix_runtime_incidents_owner_last_seen
            ON runtime_incidents(owner_id, last_seen_at DESC);
        CREATE INDEX ix_runtime_incidents_owner_status
            ON runtime_incidents(owner_id, status, last_seen_at DESC);

        ALTER TABLE runtime_incidents ENABLE ROW LEVEL SECURITY;
        ALTER TABLE runtime_incidents FORCE ROW LEVEL SECURITY;
        CREATE POLICY runtime_incidents_isolation ON runtime_incidents
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE ON runtime_incidents TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS runtime_incidents_isolation ON runtime_incidents;
        DROP TABLE IF EXISTS runtime_incidents;
        """
    )
