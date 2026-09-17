"""Founder-only MainAI boot state, covenant and status stream."""
from alembic import op
import sqlalchemy as sa

revision = "0078_founder_boot"
down_revision = "0077_supervision_telemetry"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "mainai_founder_covenants",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("covenant_hash", sa.String(128), nullable=False),
        sa.Column("clauses", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("invariants", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_by", sa.String(128), nullable=False, server_default="system"),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status in ('ACTIVE','SUPERSEDED','REVOKED')", name="ck_founder_covenant_status"),
        sa.UniqueConstraint("id", "owner_id", name="uq_founder_covenants_id_owner"),
    )
    op.create_index("ix_founder_covenant_owner", "mainai_founder_covenants", ["owner_id"])
    op.create_index(
        "uq_founder_covenant_active_owner",
        "mainai_founder_covenants",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "mainai_founder_boots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("boot_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("mainai_id", sa.String(128), nullable=False),
        sa.Column("founder_id", sa.UUID(), nullable=False),
        sa.Column("system_instance_id", sa.String(128), nullable=False),
        sa.Column("covenant_id", sa.UUID(), nullable=False),
        sa.Column("covenant_version", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False, server_default="FOUNDER_ONLY"),
        sa.Column("status", sa.String(32), nullable=False, server_default="BOOTING"),
        sa.Column("recall_status", sa.String(48), nullable=False, server_default="DISABLED_BY_SECURITY_GATE"),
        sa.Column("status_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("component_manifest", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("readiness_matrix", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("authority_profile", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("presence_state", sa.String(32), nullable=False, server_default="BOOTING"),
        sa.Column("active_program_id", sa.UUID(), nullable=True),
        sa.Column("audit_summary", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("mode = 'FOUNDER_ONLY'", name="ck_founder_boot_mode"),
        sa.CheckConstraint("status in ('BOOTING','READY','LIMITED','BLOCKED','FAULT','SHUTTING_DOWN','STOPPED')", name="ck_founder_boot_status"),
        sa.UniqueConstraint("boot_id", "owner_id", name="uq_founder_boots_boot_owner"),
        sa.ForeignKeyConstraint(["covenant_id", "owner_id"], ["mainai_founder_covenants.id", "mainai_founder_covenants.owner_id"]),
    )
    op.create_index("ix_founder_boot_owner", "mainai_founder_boots", ["owner_id"])
    op.create_table(
        "mainai_founder_boot_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("boot_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("boot_id", "sequence", name="uq_founder_boot_event_sequence"),
        sa.ForeignKeyConstraint(["boot_id", "owner_id"], ["mainai_founder_boots.boot_id", "mainai_founder_boots.owner_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_founder_boot_event_owner", "mainai_founder_boot_events", ["owner_id", "boot_id", "sequence"])
    op.create_table(
        "mainai_founder_boot_status",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("boot_id", sa.UUID(), nullable=False),
        sa.Column("presence_state", sa.String(32), nullable=False),
        sa.Column("status_payload", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["boot_id", "owner_id"], ["mainai_founder_boots.boot_id", "mainai_founder_boots.owner_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_founder_boot_status_owner_current", "mainai_founder_boot_status", ["owner_id", "is_current"])
    for table, policy in (
        ("mainai_founder_covenants", "founder_covenant_owner"),
        ("mainai_founder_boots", "founder_boot_owner"),
        ("mainai_founder_boot_events", "founder_boot_event_owner"),
        ("mainai_founder_boot_status", "founder_boot_status_owner"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {policy} ON {table}
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON mainai_founder_covenants TO mainai_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON mainai_founder_boots TO mainai_app")
    op.execute("GRANT SELECT, INSERT ON mainai_founder_boot_events TO mainai_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON mainai_founder_boot_status TO mainai_app")


def downgrade() -> None:
    for table, policy in (
        ("mainai_founder_boot_status", "founder_boot_status_owner"),
        ("mainai_founder_boot_events", "founder_boot_event_owner"),
        ("mainai_founder_boots", "founder_boot_owner"),
        ("mainai_founder_covenants", "founder_covenant_owner"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.drop_index("ix_founder_boot_status_owner_current", table_name="mainai_founder_boot_status")
    op.drop_table("mainai_founder_boot_status")
    op.drop_index("ix_founder_boot_event_owner", table_name="mainai_founder_boot_events")
    op.drop_table("mainai_founder_boot_events")
    op.drop_index("ix_founder_boot_owner", table_name="mainai_founder_boots")
    op.drop_table("mainai_founder_boots")
    op.drop_index("uq_founder_covenant_active_owner", table_name="mainai_founder_covenants")
    op.drop_index("ix_founder_covenant_owner", table_name="mainai_founder_covenants")
    op.drop_table("mainai_founder_covenants")
