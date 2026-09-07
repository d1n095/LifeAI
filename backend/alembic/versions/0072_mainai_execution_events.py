"""Canonical owner-scoped execution event outbox."""
from alembic import op
import sqlalchemy as sa

revision = "0072_mainai_exec_events"
down_revision = "0071_recall_delivery_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mainai_execution_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("mainai_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("attempt_id", sa.String(128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.String(256), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('pending','retrying','delivered','dead_letter')", name="ck_mainai_exec_event_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_mainai_exec_event_attempts"),
    )
    op.create_index("ix_mainai_exec_event_pending", "mainai_execution_events", ["owner_id", "state", "created_at"])
    op.execute("ALTER TABLE mainai_execution_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mainai_execution_events FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY mainai_execution_events_owner ON mainai_execution_events
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""")
    op.execute("REVOKE ALL ON mainai_execution_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON mainai_execution_events TO mainai_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS mainai_execution_events_owner ON mainai_execution_events")
    op.drop_index("ix_mainai_exec_event_pending", table_name="mainai_execution_events")
    op.drop_table("mainai_execution_events")
