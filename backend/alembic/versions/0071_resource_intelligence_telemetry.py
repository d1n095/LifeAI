"""MainAI Resource Intelligence Part 1 -- agent resource telemetry samples.

One small, additive table: `agent_resource_telemetry_samples` -- one row per point-in-time
observation of ONE agent dispatch attempt's resource usage, correlated with the REAL
`agent_dispatch_executions.attempt_id` (migration 0047) -- never a parallel session identity.
Every numeric column is nullable; a caller records only what it actually observed.

Structural precedent followed: `agent_dispatch_executions` (migration 0047), NOT
`agent_work_assignment_events` (migration 0046) -- an ordinary, RLS-isolated, owner-scoped
mutable table with NO deny-mutation trigger and NO mainai_app privilege-narrowing. This
table's own service layer (`app.resource_intelligence.telemetry`) only ever INSERTs by
convention, but a DB-level append-only enforcement trigger is deliberately NOT added here:
this table's `(assignment_id, owner_id)` FK cascades from `agent_work_assignments`, which is
cleaned up by the EXISTING `erase_own_agent_coordination_children()` SECURITY DEFINER function
(migrations 0046/0047, owned by `app.agent_coordination`, out of this migration's scope to
touch). A BEFORE DELETE deny-mutation trigger on this table would fire during that existing
function's own cascade-triggered delete of `agent_work_assignments` rows and abort it -- a real
regression risk for a package this round is expressly forbidden from modifying.
`agent_dispatch_executions` sidesteps this exact trap the same way, by carrying no
deny-mutation trigger of its own; this table follows that precedent deliberately.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the full architecture
decision this migration implements.
"""

from alembic import op

revision = "0071"
down_revision = "0070"
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
