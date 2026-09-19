"""MainAI Research, Truth & Advisory Intelligence -- durable research ledger. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

New, additive tables only -- no existing table/row touched. Confidence-history and
reopen-events are append-only, reusing the EXISTING `intelligence_governance_deny_mutation()`
trigger function (migration 0038) verbatim rather than defining a second one.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0073_mainai_research_ledger"
down_revision = "0072_mainai_vision_vocabulary"
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
        "mainai_research_investigations",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("saturation_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active','saturated_for_now','closed')", name="ck_research_investigations_status"),
        sa.CheckConstraint("length(btrim(question)) > 0", name="ck_research_investigations_question"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_research_investigations_idem"),
        sa.UniqueConstraint("id", "owner_id", name="uq_research_investigations_id_owner"),
    )
    op.create_index("ix_research_investigations_owner_status", "mainai_research_investigations", ["owner_id", "status"])

    op.create_table(
        "mainai_research_hypotheses",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("current_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("falsification_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["investigation_id", "owner_id"],
            ["mainai_research_investigations.id", "mainai_research_investigations.owner_id"],
            ondelete="CASCADE", name="fk_research_hypotheses_investigation_owner",
        ),
        sa.CheckConstraint("status IN ('active','survived_falsification','contradicted','superseded','withdrawn')", name="ck_research_hypotheses_status"),
        sa.CheckConstraint("current_confidence IS NULL OR current_confidence BETWEEN 0 AND 1", name="ck_research_hypotheses_confidence"),
        sa.CheckConstraint("falsification_rounds >= 0", name="ck_research_hypotheses_rounds"),
        sa.CheckConstraint("length(btrim(statement)) > 0", name="ck_research_hypotheses_statement"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_research_hypotheses_idem"),
        sa.UniqueConstraint("id", "owner_id", name="uq_research_hypotheses_id_owner"),
    )
    op.create_index("ix_research_hypotheses_owner_investigation", "mainai_research_hypotheses", ["owner_id", "investigation_id"])

    op.create_table(
        "mainai_research_evidence_links",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hypothesis_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="unresolved"),
        sa.Column("underlying_source_id", sa.String(256), nullable=False),
        sa.Column("evidence_state", sa.String(16), nullable=False, server_default="PLAUSIBLE"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rejection_rationale", sa.Text(), nullable=True),
        sa.Column("provenance", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["hypothesis_id", "owner_id"],
            ["mainai_research_hypotheses.id", "mainai_research_hypotheses.owner_id"],
            ondelete="CASCADE", name="fk_research_evidence_links_hypothesis_owner",
        ),
        sa.CheckConstraint("role IN ('support','contradiction')", name="ck_research_evidence_links_role"),
        sa.CheckConstraint(
            "lifecycle_status IN ('rejected_as_support','proven_false','insufficient_evidence','contradicted',"
            "'superseded','deprioritized','unresolved','stale','valid_support','strong_support','verified_where_possible')",
            name="ck_research_evidence_links_lifecycle",
        ),
        # Matches app.mainai_vision.evidence.EvidenceState's own real (uppercase) values exactly
        # -- this package reuses that enum verbatim, never a lowercase re-encoding of it.
        sa.CheckConstraint(
            "evidence_state IN ('OBSERVED','DERIVED','INFERRED','PLAUSIBLE','SPECULATIVE','CONTRADICTED','VERIFIED')",
            name="ck_research_evidence_links_state",
        ),
        sa.CheckConstraint("jsonb_typeof(provenance) = 'object'", name="ck_research_evidence_links_provenance"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_research_evidence_links_idem"),
        sa.UniqueConstraint("id", "owner_id", name="uq_research_evidence_links_id_owner"),
    )
    op.create_index("ix_research_evidence_links_owner_hypothesis", "mainai_research_evidence_links", ["owner_id", "hypothesis_id"])
    op.create_index("ix_research_evidence_links_source", "mainai_research_evidence_links", ["owner_id", "underlying_source_id"])

    op.create_table(
        "mainai_research_confidence_history",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hypothesis_id", sa.UUID(), nullable=False),
        sa.Column("previous_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("new_confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_link_id", sa.UUID(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["hypothesis_id", "owner_id"],
            ["mainai_research_hypotheses.id", "mainai_research_hypotheses.owner_id"],
            ondelete="CASCADE", name="fk_research_confidence_history_hypothesis_owner",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_link_id", "owner_id"],
            ["mainai_research_evidence_links.id", "mainai_research_evidence_links.owner_id"],
            ondelete="SET NULL", name="fk_research_confidence_history_evidence_owner",
        ),
        sa.CheckConstraint("new_confidence BETWEEN 0 AND 1", name="ck_research_confidence_history_new"),
        sa.CheckConstraint("previous_confidence IS NULL OR previous_confidence BETWEEN 0 AND 1", name="ck_research_confidence_history_prev"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_research_confidence_history_reason"),
    )
    op.create_index("ix_research_confidence_history_hypothesis", "mainai_research_confidence_history", ["owner_id", "hypothesis_id", "recorded_at"])

    op.create_table(
        "mainai_research_reopen_events",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_link_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("original_lifecycle_status", sa.String(32), nullable=False),
        sa.Column("new_lifecycle_status", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["evidence_link_id", "owner_id"],
            ["mainai_research_evidence_links.id", "mainai_research_evidence_links.owner_id"],
            ondelete="CASCADE", name="fk_research_reopen_events_evidence_owner",
        ),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_research_reopen_events_reason"),
    )
    op.create_index("ix_research_reopen_events_evidence", "mainai_research_reopen_events", ["owner_id", "evidence_link_id"])

    for table in (
        "mainai_research_investigations", "mainai_research_hypotheses", "mainai_research_evidence_links",
        "mainai_research_confidence_history", "mainai_research_reopen_events",
    ):
        _secure(table)

    # Append-only: reuses the EXISTING intelligence_governance_deny_mutation() trigger function
    # (migration 0038) verbatim -- never a second deny-mutation function.
    for table in ("mainai_research_confidence_history", "mainai_research_reopen_events"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_deny_mutation BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION intelligence_governance_deny_mutation()"
        )
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM mainai_app")


def downgrade() -> None:
    for table in ("mainai_research_confidence_history", "mainai_research_reopen_events"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_deny_mutation ON {table}")
    op.drop_table("mainai_research_reopen_events")
    op.drop_table("mainai_research_confidence_history")
    op.drop_table("mainai_research_evidence_links")
    op.drop_table("mainai_research_hypotheses")
    op.drop_table("mainai_research_investigations")
