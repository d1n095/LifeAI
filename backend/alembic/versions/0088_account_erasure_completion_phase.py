"""Require ordered erasure phases before operation completion.

Revision ID: 0088_erasure_completion_phase
Revises: 0087_relationship_vocabulary
Create Date: 2026-09-24
"""

from alembic import op

revision = "0088_erasure_completion_phase"
down_revision = "0087_relationship_vocabulary"
branch_labels = None
depends_on = None


_ORDERED_SET_PHASE = """
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
    AND (
      (op.phase = 'started' AND p_phase = 'personal_recall_erasure')
      OR (op.phase = 'personal_recall_erasure' AND p_phase = 'personal_data_erasure')
    )
    AND op.reauth_receipt_id = rr.receipt_id
    AND rr.owner_id = p_owner_id
    AND rr.consumed_by_operation_id = op.operation_id
    AND rr.consumed_at IS NOT NULL
    AND public.account_erasure_receipt_session_current(p_owner_id, rr.access_jti);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'account erasure phase transition is invalid or lacks current reauth authority';
  END IF;
  RETURN true;
END $$;
"""

_ORDERED_COMPLETE = """
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
    AND op.phase = 'personal_data_erasure'
    AND op.reauth_receipt_id = rr.receipt_id
    AND rr.owner_id = p_owner_id
    AND rr.consumed_by_operation_id = op.operation_id
    AND rr.consumed_at IS NOT NULL
    AND public.account_erasure_receipt_session_current(p_owner_id, rr.access_jti);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'account erasure operation is not ready for completion';
  END IF;
  RETURN true;
END $$;
"""

_PREVIOUS_SET_PHASE = _ORDERED_SET_PHASE.replace(
    "    AND (\n"
    "      (op.phase = 'started' AND p_phase = 'personal_recall_erasure')\n"
    "      OR (op.phase = 'personal_recall_erasure' AND p_phase = 'personal_data_erasure')\n"
    "    )\n",
    "",
).replace(
    "account erasure phase transition is invalid or lacks current reauth authority",
    "account erasure phase transition lacks current reauth authority",
)

_PREVIOUS_COMPLETE = _ORDERED_COMPLETE.replace("    AND op.phase = 'personal_data_erasure'\n", "").replace(
    "account erasure operation is not ready for completion",
    "account erasure operation is not active",
)


def _apply_functions(set_phase_sql: str, complete_sql: str) -> None:
    op.execute(set_phase_sql)
    op.execute(complete_sql)
    op.execute("REVOKE ALL ON FUNCTION account_erasure_set_phase(uuid, uuid, text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION account_erasure_complete_operation(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION account_erasure_set_phase(uuid, uuid, text) TO mainai_app")
    op.execute("GRANT EXECUTE ON FUNCTION account_erasure_complete_operation(uuid, uuid) TO mainai_app")


def upgrade() -> None:
    _apply_functions(_ORDERED_SET_PHASE, _ORDERED_COMPLETE)


def downgrade() -> None:
    _apply_functions(_PREVIOUS_SET_PHASE, _PREVIOUS_COMPLETE)
