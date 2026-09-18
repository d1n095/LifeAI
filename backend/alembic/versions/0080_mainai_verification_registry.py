"""MainAI independent verification registry.

Append-only exact-SHA examiner attestations. These records are evidence for readiness only;
they do not grant execution, merge, deploy, provider, or recall authority.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0080_verification_registry"
down_revision = "0079_mainai_workforce_mastery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mainai_verification_records",
        sa.Column("verification_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("component_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(160), nullable=False),
        sa.Column("candidate_sha", sa.String(40), nullable=False),
        sa.Column("candidate_tree_identity", sa.String(160), nullable=True),
        sa.Column("builder_identity", sa.String(128), nullable=False),
        sa.Column("examiner_identity", sa.String(128), nullable=False),
        sa.Column("examiner_class", sa.String(64), nullable=False, server_default="external_agent"),
        sa.Column("independence_relationship", sa.String(64), nullable=False, server_default="different_agent"),
        sa.Column("identity_assurance", sa.String(32), nullable=False, server_default="asserted"),
        sa.Column("review_type", sa.String(64), nullable=False, server_default="independent_review"),
        sa.Column("review_result", sa.String(16), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("verification_scope", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_summary", sa.Text(), nullable=False),
        sa.Column("test_evidence_refs", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_provenance", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("supersedes_verification_id", sa.UUID(), nullable=True),
        sa.Column("invalidates_verification_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("candidate_sha ~ '^[0-9a-f]{40}$'", name="ck_verification_candidate_sha"),
        sa.CheckConstraint("review_result IN ('PASS','FAIL','BLOCKED')", name="ck_verification_result"),
        sa.CheckConstraint("length(btrim(component_id)) > 0", name="ck_verification_component_nonempty"),
        sa.CheckConstraint("length(btrim(candidate_id)) > 0", name="ck_verification_candidate_nonempty"),
        sa.CheckConstraint("length(btrim(builder_identity)) > 0", name="ck_verification_builder_nonempty"),
        sa.CheckConstraint("length(btrim(examiner_identity)) > 0", name="ck_verification_examiner_nonempty"),
        sa.CheckConstraint("builder_identity <> examiner_identity", name="ck_verification_no_self_certification"),
        sa.CheckConstraint("jsonb_typeof(verification_scope) = 'object'", name="ck_verification_scope_object"),
        sa.CheckConstraint("jsonb_typeof(test_evidence_refs) = 'array'", name="ck_verification_tests_array"),
        sa.CheckConstraint("jsonb_typeof(source_provenance) = 'object'", name="ck_verification_source_object"),
    )
    op.create_index("ix_verification_component_sha_result", "mainai_verification_records", ["component_id", "candidate_sha", "review_result"])
    op.create_index("ix_verification_owner_component", "mainai_verification_records", ["owner_id", "component_id"])
    op.create_index("ix_verification_candidate", "mainai_verification_records", ["candidate_id"])

    op.execute("ALTER TABLE mainai_verification_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mainai_verification_records FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY mainai_verification_records_read ON mainai_verification_records
        USING (owner_id IS NULL OR owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (false)""")
    op.execute("REVOKE ALL ON mainai_verification_records FROM PUBLIC")
    op.execute("GRANT SELECT ON mainai_verification_records TO mainai_app")

    op.execute("""CREATE OR REPLACE FUNCTION mainai_verification_records_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'mainai_verification_records is append-only';
        END IF;
        IF TG_OP = 'INSERT' AND session_user = 'mainai_app' THEN
            RAISE EXCEPTION 'ordinary MainAI runtime cannot create verification records';
        END IF;
        RETURN NEW;
    END;
    $$""")
    op.execute("REVOKE ALL ON FUNCTION mainai_verification_records_guard() FROM PUBLIC")
    op.execute("""CREATE TRIGGER trg_mainai_verification_records_guard
        BEFORE INSERT OR UPDATE OR DELETE ON mainai_verification_records
        FOR EACH ROW EXECUTE FUNCTION mainai_verification_records_guard()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_mainai_verification_records_guard ON mainai_verification_records")
    op.execute("DROP FUNCTION IF EXISTS mainai_verification_records_guard()")
    op.drop_table("mainai_verification_records")
