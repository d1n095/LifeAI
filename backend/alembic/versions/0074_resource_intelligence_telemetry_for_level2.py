"""Resource Intelligence telemetry table integrated into the Level-2 composition branch.

Ported from verified Resource Intelligence candidate
063c2569a170ccc3eb7887eadd2ed1b7caed73ff:backend/alembic/versions/0071_resource_intelligence_telemetry.py.

The revision id is new because this branch already uses 0071-0073 for Personal Recall,
execution events, and Level-2 program contracts. The table definition and RLS semantics remain
source-equivalent: owner-scoped telemetry samples tied to real agent assignments; missing
metrics remain nullable and never become authority.
"""
from alembic import op

revision = "0074_resource_intel"
down_revision = "0073_mainai_level2_programs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_resource_telemetry_samples (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            sampled_at timestamp NOT NULL DEFAULT now(),
            context_used_tokens integer,
            context_window_tokens integer,
            input_tokens integer,
            output_tokens integer,
            cached_tokens integer,
            tool_calls integer,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT fk_agent_resource_telemetry_samples_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_agent_resource_telemetry_samples_nonneg CHECK (
                (context_used_tokens IS NULL OR context_used_tokens >= 0) AND
                (context_window_tokens IS NULL OR context_window_tokens >= 0) AND
                (input_tokens IS NULL OR input_tokens >= 0) AND
                (output_tokens IS NULL OR output_tokens >= 0) AND
                (cached_tokens IS NULL OR cached_tokens >= 0) AND
                (tool_calls IS NULL OR tool_calls >= 0)
            ),
            CONSTRAINT ck_agent_resource_telemetry_samples_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_agent_resource_telemetry_samples_owner_assignment
            ON agent_resource_telemetry_samples(owner_id, assignment_id);
        CREATE INDEX ix_agent_resource_telemetry_samples_attempt_sampled
            ON agent_resource_telemetry_samples(attempt_id, sampled_at);
    """)

    op.execute("""
        ALTER TABLE agent_resource_telemetry_samples ENABLE ROW LEVEL SECURITY;
        ALTER TABLE agent_resource_telemetry_samples FORCE ROW LEVEL SECURITY;
        CREATE POLICY agent_resource_telemetry_samples_isolation ON agent_resource_telemetry_samples
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE agent_resource_telemetry_samples;")
