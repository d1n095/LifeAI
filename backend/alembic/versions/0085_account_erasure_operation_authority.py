"""Bind Personal Recall erasure to canonical account-erasure operation state.

Revision ID: 0085_account_erasure_authority
Revises: 0084_recall_erasure_lifecycle
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0085_account_erasure_authority"
down_revision = "0084_recall_erasure_lifecycle"
branch_labels = None
depends_on = None


_OWNER = "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "account_erasure_operations",
        sa.Column("operation_id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("phase", sa.String(64), nullable=False, server_default="started"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active','completed','failed','cancelled')", name="ck_account_erasure_operation_status"),
        sa.CheckConstraint("phase <> ''", name="ck_account_erasure_operation_phase_nonempty"),
    )
    op.create_index("ix_account_erasure_operations_owner", "account_erasure_operations", ["owner_id"])
    op.create_index("ix_account_erasure_operations_status", "account_erasure_operations", ["status"])
    op.execute("ALTER TABLE account_erasure_operations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE account_erasure_operations FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY account_erasure_operations_owner ON account_erasure_operations "
        f"USING ({_OWNER}) WITH CHECK ({_OWNER})"
    )
    op.execute("REVOKE ALL ON account_erasure_operations FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON account_erasure_operations TO mainai_app")

    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_erasure_current(p_owner_id uuid)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
      v_operation_id uuid := NULLIF(current_setting('app.account_erasure_operation_id', true), '')::uuid;
    BEGIN
      IF current_setting('app.personal_recall_erasure_in_progress', true) <> 'true'
         OR v_current_owner IS NULL
         OR v_current_owner IS DISTINCT FROM p_owner_id
         OR v_operation_id IS NULL THEN
        RETURN false;
      END IF;

      RETURN EXISTS (
        SELECT 1
        FROM account_erasure_operations op
        WHERE op.operation_id = v_operation_id
          AND op.owner_id = p_owner_id
          AND op.status = 'active'
          AND op.phase = 'personal_recall_erasure'
      );
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_erasure_current(p_owner_id uuid)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    BEGIN
      RETURN current_setting('app.personal_recall_erasure_in_progress', true) = 'true'
         AND v_current_owner IS NOT NULL
         AND v_current_owner = p_owner_id;
    END $$;
    """)
    op.drop_index("ix_account_erasure_operations_status", table_name="account_erasure_operations")
    op.drop_index("ix_account_erasure_operations_owner", table_name="account_erasure_operations")
    op.drop_table("account_erasure_operations")
