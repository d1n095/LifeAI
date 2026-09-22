"""Bind account erasure operations to recent password reauthentication.

Revision ID: 0086_account_erasure_reauth
Revises: 0085_account_erasure_authority
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0086_account_erasure_reauth"
down_revision = "0085_account_erasure_authority"
branch_labels = None
depends_on = None

_OWNER = "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "account_erasure_reauth_receipts",
        sa.Column("receipt_id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_jti", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False, server_default="ACCOUNT_ERASURE"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_operation_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("purpose = 'ACCOUNT_ERASURE'", name="ck_account_erasure_reauth_purpose"),
    )
    op.create_index("ix_account_erasure_reauth_owner", "account_erasure_reauth_receipts", ["owner_id"])
    op.create_index("ix_account_erasure_reauth_access_jti", "account_erasure_reauth_receipts", ["access_jti"])
    op.create_index("ix_account_erasure_reauth_consumed", "account_erasure_reauth_receipts", ["consumed_at"])
    op.execute("ALTER TABLE account_erasure_reauth_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE account_erasure_reauth_receipts FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY account_erasure_reauth_receipts_owner ON account_erasure_reauth_receipts "
        f"USING ({_OWNER}) WITH CHECK (false)"
    )
    op.execute("REVOKE ALL ON account_erasure_reauth_receipts FROM PUBLIC")
    op.execute("GRANT SELECT ON account_erasure_reauth_receipts TO mainai_app")

    op.add_column("account_erasure_operations", sa.Column("reauth_receipt_id", sa.UUID(), nullable=True))
    op.add_column("account_erasure_operations", sa.Column("access_jti", sa.String(36), nullable=True))
    op.create_index("ix_account_erasure_operations_reauth", "account_erasure_operations", ["reauth_receipt_id"], unique=True)
    op.create_foreign_key(
        "fk_account_erasure_operations_reauth_receipt",
        "account_erasure_operations",
        "account_erasure_reauth_receipts",
        ["reauth_receipt_id"],
        ["receipt_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_account_erasure_reauth_consumed_operation",
        "account_erasure_reauth_receipts",
        "account_erasure_operations",
        ["consumed_by_operation_id"],
        ["operation_id"],
        ondelete="SET NULL",
    )

    op.execute("REVOKE INSERT, UPDATE, DELETE ON account_erasure_operations FROM mainai_app")
    op.execute("GRANT SELECT ON account_erasure_operations TO mainai_app")

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_reauth_receipts_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF current_user = 'mainai_app' THEN
        RAISE EXCEPTION 'account erasure reauth receipts are governed; raw runtime writes are forbidden';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_account_erasure_reauth_receipts_guard
      BEFORE INSERT OR UPDATE OR DELETE ON account_erasure_reauth_receipts
      FOR EACH ROW EXECUTE FUNCTION account_erasure_reauth_receipts_guard()
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_operations_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF current_user = 'mainai_app' THEN
        RAISE EXCEPTION 'account erasure operations are governed; use account erasure functions';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_account_erasure_operations_guard
      BEFORE INSERT OR UPDATE OR DELETE ON account_erasure_operations
      FOR EACH ROW EXECUTE FUNCTION account_erasure_operations_guard()
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_receipt_session_current(p_owner_id uuid, p_access_jti text)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
      v_current_access_jti text := NULLIF(current_setting('app.current_access_jti', true), '');
    BEGIN
      IF v_current_owner IS NULL
         OR v_current_owner IS DISTINCT FROM p_owner_id
         OR p_access_jti IS NULL
         OR p_access_jti = ''
         OR v_current_access_jti IS NULL
         OR v_current_access_jti IS DISTINCT FROM p_access_jti THEN
        RETURN false;
      END IF;
      RETURN EXISTS (
        SELECT 1
        FROM public.users u
        JOIN public.refresh_tokens rt ON rt.user_id = u.id AND rt.access_jti = p_access_jti
        WHERE u.id = p_owner_id
          AND u.is_active IS TRUE
          AND NOT EXISTS (SELECT 1 FROM public.revoked_access_tokens rat WHERE rat.jti = p_access_jti)
          AND rt.created_at > u.sessions_valid_after
      );
    END $$;
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_begin_operation(p_owner_id uuid, p_receipt_id uuid)
    RETURNS uuid
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
      v_operation_id uuid := pg_catalog.gen_random_uuid();
      v_receipt public.account_erasure_reauth_receipts%ROWTYPE;
    BEGIN
      IF v_current_owner IS NULL OR v_current_owner IS DISTINCT FROM p_owner_id THEN
        RAISE EXCEPTION 'account erasure operation requires current owner context';
      END IF;

      SELECT * INTO v_receipt
      FROM public.account_erasure_reauth_receipts
      WHERE receipt_id = p_receipt_id
        AND owner_id = p_owner_id
        AND purpose = 'ACCOUNT_ERASURE'
      FOR UPDATE;

      IF NOT FOUND THEN
        RAISE EXCEPTION 'account erasure reauth receipt is missing or wrong owner';
      END IF;
      IF v_receipt.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'account erasure reauth receipt is already consumed';
      END IF;
      IF v_receipt.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'account erasure reauth receipt is expired';
      END IF;
      IF v_receipt.issued_at > clock_timestamp()
         OR v_receipt.issued_at <= clock_timestamp() - interval '5 minutes'
         OR v_receipt.expires_at > v_receipt.issued_at + interval '5 minutes' THEN
        RAISE EXCEPTION 'account erasure reauth receipt is outside the allowed lifetime';
      END IF;
      IF NOT public.account_erasure_receipt_session_current(p_owner_id, v_receipt.access_jti) THEN
        RAISE EXCEPTION 'account erasure reauth receipt is not bound to a current session';
      END IF;

      INSERT INTO public.account_erasure_operations(operation_id, owner_id, status, phase, reauth_receipt_id, access_jti)
      VALUES (v_operation_id, p_owner_id, 'active', 'started', p_receipt_id, v_receipt.access_jti);

      UPDATE public.account_erasure_reauth_receipts
      SET consumed_at = now(), consumed_by_operation_id = v_operation_id
      WHERE receipt_id = p_receipt_id AND consumed_at IS NULL;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'account erasure reauth receipt was concurrently consumed';
      END IF;

      RETURN v_operation_id;
    END $$;
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_set_phase(p_operation_id uuid, p_owner_id uuid, p_phase text)
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    BEGIN
      IF v_current_owner IS NULL OR v_current_owner IS DISTINCT FROM p_owner_id THEN
        RAISE EXCEPTION 'account erasure phase transition requires current owner context';
      END IF;
      IF p_phase NOT IN ('personal_recall_erasure', 'personal_data_erasure') THEN
        RAISE EXCEPTION 'unsupported account erasure phase';
      END IF;

      UPDATE public.account_erasure_operations op
      SET phase = p_phase, updated_at = now()
      FROM public.account_erasure_reauth_receipts rr
      WHERE op.operation_id = p_operation_id
        AND op.owner_id = p_owner_id
        AND op.status = 'active'
        AND op.reauth_receipt_id = rr.receipt_id
        AND rr.owner_id = p_owner_id
        AND rr.consumed_by_operation_id = op.operation_id
        AND rr.consumed_at IS NOT NULL
        AND public.account_erasure_receipt_session_current(p_owner_id, rr.access_jti);
      IF NOT FOUND THEN
        RAISE EXCEPTION 'account erasure phase transition lacks current reauth authority';
      END IF;
      RETURN true;
    END $$;
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION account_erasure_complete_operation(p_operation_id uuid, p_owner_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    BEGIN
      IF v_current_owner IS NULL OR v_current_owner IS DISTINCT FROM p_owner_id THEN
        RAISE EXCEPTION 'account erasure completion requires current owner context';
      END IF;
      UPDATE public.account_erasure_operations op
      SET status = 'completed', phase = 'completed', updated_at = now()
      FROM public.account_erasure_reauth_receipts rr
      WHERE op.operation_id = p_operation_id
        AND op.owner_id = p_owner_id
        AND op.status = 'active'
        AND op.reauth_receipt_id = rr.receipt_id
        AND rr.owner_id = p_owner_id
        AND rr.consumed_by_operation_id = op.operation_id
        AND rr.consumed_at IS NOT NULL
        AND public.account_erasure_receipt_session_current(p_owner_id, rr.access_jti);
      IF NOT FOUND THEN
        RAISE EXCEPTION 'account erasure operation is not active';
      END IF;
      RETURN true;
    END $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION account_erasure_begin_operation(uuid, uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION account_erasure_set_phase(uuid, uuid, text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION account_erasure_complete_operation(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION account_erasure_begin_operation(uuid, uuid) TO mainai_app")
    op.execute("GRANT EXECUTE ON FUNCTION account_erasure_set_phase(uuid, uuid, text) TO mainai_app")
    op.execute("GRANT EXECUTE ON FUNCTION account_erasure_complete_operation(uuid, uuid) TO mainai_app")

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
        FROM public.account_erasure_operations op
        JOIN public.account_erasure_reauth_receipts rr ON rr.receipt_id = op.reauth_receipt_id
        WHERE op.operation_id = v_operation_id
          AND op.owner_id = p_owner_id
          AND op.status = 'active'
          AND op.phase = 'personal_recall_erasure'
          AND rr.owner_id = p_owner_id
          AND rr.consumed_by_operation_id = op.operation_id
          AND rr.consumed_at IS NOT NULL
          AND public.account_erasure_receipt_session_current(p_owner_id, rr.access_jti)
      );
    END $$;
    """)


def downgrade() -> None:
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
        SELECT 1 FROM account_erasure_operations op
        WHERE op.operation_id = v_operation_id
          AND op.owner_id = p_owner_id
          AND op.status = 'active'
          AND op.phase = 'personal_recall_erasure'
      );
    END $$;
    """)
    op.execute("DROP FUNCTION IF EXISTS account_erasure_complete_operation(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS account_erasure_set_phase(uuid, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS account_erasure_begin_operation(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS account_erasure_receipt_session_current(uuid, text)")
    op.execute("DROP TRIGGER IF EXISTS trg_account_erasure_operations_guard ON account_erasure_operations")
    op.execute("DROP FUNCTION IF EXISTS account_erasure_operations_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_account_erasure_reauth_receipts_guard ON account_erasure_reauth_receipts")
    op.execute("DROP FUNCTION IF EXISTS account_erasure_reauth_receipts_guard()")
    op.drop_constraint("fk_account_erasure_reauth_consumed_operation", "account_erasure_reauth_receipts", type_="foreignkey")
    op.drop_constraint("fk_account_erasure_operations_reauth_receipt", "account_erasure_operations", type_="foreignkey")
    op.drop_index("ix_account_erasure_operations_reauth", table_name="account_erasure_operations")
    op.drop_column("account_erasure_operations", "access_jti")
    op.drop_column("account_erasure_operations", "reauth_receipt_id")
    op.drop_index("ix_account_erasure_reauth_consumed", table_name="account_erasure_reauth_receipts")
    op.drop_index("ix_account_erasure_reauth_access_jti", table_name="account_erasure_reauth_receipts")
    op.drop_index("ix_account_erasure_reauth_owner", table_name="account_erasure_reauth_receipts")
    op.drop_table("account_erasure_reauth_receipts")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON account_erasure_operations TO mainai_app")
