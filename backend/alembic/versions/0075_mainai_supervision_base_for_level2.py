"""Canonical PostgreSQL persistence for continuous agent supervision."""
from alembic import op
import sqlalchemy as sa

revision = "0075_supervision_base"
down_revision = "0074_resource_intel"
branch_labels = None
depends_on = None


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""CREATE POLICY {table}_owner ON {table}
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""")
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mainai_app")


def upgrade() -> None:
    op.create_table(
        "mainai_supervision_agents",
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("process_nonce", sa.String(128), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempt_id", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("progress_key", sa.String(256), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state <> ''", name="ck_supervision_agent_state"),
    )
    op.create_index("ix_supervision_agents_owner_state", "mainai_supervision_agents", ["owner_id", "state"])
    op.create_index("ix_supervision_agents_owner_job", "mainai_supervision_agents", ["owner_id", "job_id"])

    op.create_table(
        "mainai_supervision_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("mainai_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "job_id", "kind", "sequence", name="uq_mainai_supervision_message"),
        sa.CheckConstraint("state IN ('pending','retrying','delivered','dead_letter')", name="ck_supervision_message_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_supervision_message_attempts"),
        sa.CheckConstraint("sequence >= 1", name="ck_supervision_message_sequence"),
    )
    op.create_index("ix_supervision_messages_pending", "mainai_supervision_messages", ["owner_id", "state", "created_at"])

    op.create_table(
        "mainai_budget_reservations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("mainai_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("attempt_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_mainai_budget_amount"),
        sa.CheckConstraint("state IN ('reserved','bound','settled','released','expired')", name="ck_mainai_budget_state"),
    )
    op.create_index("ix_mainai_budget_owner_state", "mainai_budget_reservations", ["owner_id", "state"])
    for table in ("mainai_supervision_agents", "mainai_supervision_messages", "mainai_budget_reservations"):
        _secure(table)


def downgrade() -> None:
    for table in ("mainai_budget_reservations", "mainai_supervision_messages", "mainai_supervision_agents"):
        op.execute(f"DROP POLICY IF EXISTS {table}_owner ON {table}")
    op.drop_index("ix_mainai_budget_owner_state", table_name="mainai_budget_reservations")
    op.drop_table("mainai_budget_reservations")
    op.drop_index("ix_supervision_messages_pending", table_name="mainai_supervision_messages")
    op.drop_table("mainai_supervision_messages")
    op.drop_index("ix_supervision_agents_owner_job", table_name="mainai_supervision_agents")
    op.drop_index("ix_supervision_agents_owner_state", table_name="mainai_supervision_agents")
    op.drop_table("mainai_supervision_agents")
