"""Canonical Level-2 program contracts and append-only recovery journal."""
from alembic import op
import sqlalchemy as sa

revision = "0073_mainai_level2_programs"
down_revision = "0072_mainai_exec_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mainai_level2_programs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("verification_criteria", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("authority_boundary", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("dependency_graph", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.String(32), nullable=False, server_default="RUNNING"),
        sa.Column("remaining_work", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("current_sha", sa.String(64)), sa.Column("builder_id", sa.String(128)),
        sa.Column("examiner_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("budget_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("stop_conditions", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("founder_only_decisions", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("handoff_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("currentness_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_level2_program_owner", "mainai_level2_programs", ["owner_id"])
    op.create_table(
        "mainai_level2_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("program_id", sa.UUID(), sa.ForeignKey("mainai_level2_programs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("program_id", "sequence", name="uq_level2_event_sequence"),
    )
    op.create_index("ix_level2_event_owner", "mainai_level2_events", ["owner_id", "program_id", "sequence"])
    for table, policy in (("mainai_level2_programs", "level2_program_owner"), ("mainai_level2_events", "level2_event_owner")):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {policy} ON {table}
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON mainai_level2_programs TO mainai_app")
    op.execute("GRANT SELECT, INSERT ON mainai_level2_events TO mainai_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS level2_event_owner ON mainai_level2_events")
    op.execute("DROP POLICY IF EXISTS level2_program_owner ON mainai_level2_programs")
    op.drop_index("ix_level2_event_owner", table_name="mainai_level2_events")
    op.drop_table("mainai_level2_events")
    op.drop_index("ix_level2_program_owner", table_name="mainai_level2_programs")
    op.drop_table("mainai_level2_programs")
