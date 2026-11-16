"""MainAI Cognitive Efficiency + Systemic Debugging + Situational Awareness + Information
Lifecycle + Repo/Backup Intelligence -- durable Founder Communication Ledger, plus a
queryability index supporting the deliberate decision to keep the research investigation
graph in the existing `mainai_research_evidence_links.provenance` JSONB column rather than a
new relational table. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

New, additive objects only -- no existing table/row touched. The communication ledger is
append-only (never edited/deleted), reusing the EXISTING
`intelligence_governance_deny_mutation()` trigger function (migration 0038) verbatim.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0074_mainai_cognitive_ops"
down_revision = "0073_mainai_research_ledger"
branch_labels = None
depends_on = None


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY {table}_owner ON {table}
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)"""
    )
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mainai_app")


def upgrade() -> None:
    op.create_table(
        "mainai_ops_founder_communications",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("status_communicated", sa.Text(), nullable=False),
        sa.Column("material_facts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("decision_requested", sa.Text(), nullable=True),
        sa.Column("decision_received", sa.Text(), nullable=True),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("communicated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("length(btrim(topic)) > 0", name="ck_ops_founder_comm_topic"),
        sa.CheckConstraint("length(btrim(status_communicated)) > 0", name="ck_ops_founder_comm_status"),
        sa.CheckConstraint("jsonb_typeof(material_facts) = 'object'", name="ck_ops_founder_comm_facts"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_ops_founder_comm_idem"),
        sa.UniqueConstraint("id", "owner_id", name="uq_ops_founder_comm_id_owner"),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "owner_id"], ["mainai_ops_founder_communications.id", "mainai_ops_founder_communications.owner_id"],
            name="fk_ops_founder_comm_supersedes",
        ),
    )
    op.create_index("ix_ops_founder_comm_owner_topic", "mainai_ops_founder_communications", ["owner_id", "topic", "communicated_at"])
    _secure("mainai_ops_founder_communications")

    # Append-only: reuses the EXISTING trigger function from migration 0038, never redefined.
    op.execute(
        """CREATE TRIGGER trg_mainai_ops_founder_communications_deny_mutation
        BEFORE UPDATE OR DELETE ON mainai_ops_founder_communications
        FOR EACH ROW EXECUTE FUNCTION intelligence_governance_deny_mutation()"""
    )
    op.execute("REVOKE UPDATE, DELETE ON mainai_ops_founder_communications FROM mainai_app")

    # Queryability support for the deliberate decision (see reconciliation doc §23B) to keep the
    # research investigation graph inside the existing evidence_links.provenance JSONB column
    # rather than add a new relational Actor/Relationship table.
    op.execute(
        "CREATE INDEX ix_research_evidence_links_provenance_gin "
        "ON mainai_research_evidence_links USING GIN (provenance)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_research_evidence_links_provenance_gin")
    op.execute("DROP TRIGGER IF EXISTS trg_mainai_ops_founder_communications_deny_mutation ON mainai_ops_founder_communications")
    op.drop_table("mainai_ops_founder_communications")
