"""Stage R — self_improvement_roi_records (Alembic 0069)."""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE self_improvement_roi_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            change_ref varchar(256) NOT NULL,
            metrics_before jsonb NOT NULL DEFAULT '{}'::jsonb,
            metrics_after jsonb NOT NULL DEFAULT '{}'::jsonb,
            complexity_cost double precision NOT NULL DEFAULT 0,
            net_roi double precision NOT NULL DEFAULT 0,
            recommendation varchar(64) NOT NULL DEFAULT 'observe',
            rationale text NOT NULL DEFAULT '',
            idempotency_key varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_self_improvement_roi_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_self_improvement_roi_rec CHECK (recommendation IN ('keep','revert','observe','resist_add'))
        );
        CREATE INDEX ix_self_improvement_roi_owner ON self_improvement_roi_records(owner_id);
        ALTER TABLE self_improvement_roi_records ENABLE ROW LEVEL SECURITY;
        ALTER TABLE self_improvement_roi_records FORCE ROW LEVEL SECURITY;
        CREATE POLICY self_improvement_roi_records_isolation ON self_improvement_roi_records
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS self_improvement_roi_records;")
