"""Stage L — prediction_records (Alembic 0067)."""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE prediction_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind varchar(40) NOT NULL,
            subject_ref varchar(256) NOT NULL,
            predicted_value jsonb NOT NULL DEFAULT '{}'::jsonb,
            confidence double precision NOT NULL DEFAULT 0.5,
            actual_value jsonb,
            outcome_delta jsonb,
            heuristic_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(24) NOT NULL DEFAULT 'open',
            idempotency_key varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            scored_at timestamptz,
            CONSTRAINT uq_prediction_records_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_prediction_records_kind CHECK (kind IN (
                'effort', 'risk', 'expected_result', 'likely_blocker', 'plan_success'
            )),
            CONSTRAINT ck_prediction_records_status CHECK (status IN ('open', 'scored')),
            CONSTRAINT ck_prediction_records_predicted CHECK (jsonb_typeof(predicted_value) = 'object'),
            CONSTRAINT ck_prediction_records_tags CHECK (jsonb_typeof(heuristic_tags) = 'array')
        );
        CREATE INDEX ix_prediction_records_owner ON prediction_records(owner_id);
        ALTER TABLE prediction_records ENABLE ROW LEVEL SECURITY;
        ALTER TABLE prediction_records FORCE ROW LEVEL SECURITY;
        CREATE POLICY prediction_records_isolation ON prediction_records
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prediction_records;")
