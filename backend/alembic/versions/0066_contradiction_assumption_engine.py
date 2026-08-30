"""Stage K — Contradiction + assumption engine tables (Alembic 0066)."""

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE structured_claims (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind varchar(32) NOT NULL,
            statement text NOT NULL,
            confidence double precision NOT NULL DEFAULT 0.5,
            source varchar(128) NOT NULL DEFAULT 'unknown',
            status varchar(24) NOT NULL DEFAULT 'active',
            dependent_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            last_validated_at timestamptz,
            revalidation_trigger varchar(256),
            related_entity_id uuid,
            contradicts_entity_id uuid,
            supersedes_claim_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_structured_claims_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_structured_claims_kind CHECK (kind IN (
                'contradicts', 'assumption', 'fact', 'superseded', 'context_specific'
            )),
            CONSTRAINT ck_structured_claims_status CHECK (status IN (
                'active', 'invalidated', 'superseded', 'disputed'
            )),
            CONSTRAINT ck_structured_claims_dependent_refs CHECK (jsonb_typeof(dependent_refs) = 'array'),
            CONSTRAINT ck_structured_claims_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_structured_claims_owner ON structured_claims(owner_id);
        CREATE INDEX ix_structured_claims_kind_status ON structured_claims(owner_id, kind, status);
        ALTER TABLE structured_claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE structured_claims FORCE ROW LEVEL SECURITY;
        CREATE POLICY structured_claims_isolation ON structured_claims
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE TABLE structured_claim_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            claim_id uuid NOT NULL REFERENCES structured_claims(id) ON DELETE CASCADE,
            event_type varchar(40) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_structured_claim_events_claim ON structured_claim_events(claim_id);
        ALTER TABLE structured_claim_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE structured_claim_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY structured_claim_events_isolation ON structured_claim_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS structured_claim_events;")
    op.execute("DROP TABLE IF EXISTS structured_claims;")
