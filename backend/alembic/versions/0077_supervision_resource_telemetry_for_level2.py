"""Add owner-scoped resource observations for future scheduling decisions."""
from alembic import op

revision = "0077_supervision_telemetry"
down_revision = "0076_supervision_fences"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE mainai_supervision_telemetry (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      agent_id varchar(128) NOT NULL,
      job_id uuid,
      attempt_id varchar(128),
      provider varchar(128), model varchar(128), state varchar(32) NOT NULL,
      productive_seconds numeric(18,3), idle_seconds numeric(18,3),
      blocked_seconds numeric(18,3), stalled_seconds numeric(18,3),
      continuation_count integer NOT NULL DEFAULT 0,
      premature_return_count integer NOT NULL DEFAULT 0,
      restart_count integer NOT NULL DEFAULT 0,
      context_input_tokens integer, context_output_tokens integer,
      context_cached_tokens integer, context_limit_tokens integer,
      provider_quota_remaining numeric(18,6), estimated_cost numeric(18,6),
      reported_cost numeric(18,6), validated_cost numeric(18,6),
      retries integer NOT NULL DEFAULT 0,
      failed_attempts integer NOT NULL DEFAULT 0,
      rework_count integer NOT NULL DEFAULT 0,
      examiner_outcome varchar(24), last_progress_at timestamptz,
      handoff_ready boolean NOT NULL DEFAULT false,
      context_risk varchar(24), observed_at timestamptz NOT NULL,
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      UNIQUE(owner_id,agent_id,observed_at)
    );
    CREATE INDEX ix_supervision_telemetry_owner_time
      ON mainai_supervision_telemetry(owner_id, observed_at DESC);
    ALTER TABLE mainai_supervision_telemetry ENABLE ROW LEVEL SECURITY;
    ALTER TABLE mainai_supervision_telemetry FORCE ROW LEVEL SECURITY;
    CREATE POLICY mainai_supervision_telemetry_owner ON mainai_supervision_telemetry
      USING(owner_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid)
      WITH CHECK(owner_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid);
    REVOKE ALL ON mainai_supervision_telemetry FROM PUBLIC;
    GRANT SELECT, INSERT ON mainai_supervision_telemetry TO mainai_app;
    """)


def downgrade():
    op.execute("DROP TABLE mainai_supervision_telemetry")
