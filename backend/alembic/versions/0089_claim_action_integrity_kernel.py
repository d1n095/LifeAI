"""Add append-only claim/action integrity evidence and receipt ledgers.

Revision ID: 0089_claim_action_integrity
Revises: 0088_erasure_completion_phase
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0089_claim_action_integrity"
down_revision = "0088_erasure_completion_phase"
branch_labels = None
depends_on = None

_CLAIM_STATES = "'intended','requested','dispatched','running','completed','externally_observed','independently_verified','certified','merged','deployed','activated','failed','unknown'"
_ACTION_STATES = "'intended','requested','dispatched','running','completed','failed','unknown'"
_VERIFICATION_STATES = "'unverified','self_reported','externally_observed','independently_verified','certified','stale','conflicting','invalid'"
_SOURCE_TYPES = "'github','database','filesystem','ci','test_runner','deployment_provider','task_execution_ledger','verification_registry','external_service','agent_self_report','mainai_generated_text','user_attestation','unknown'"


def upgrade() -> None:
    op.create_table(
        "claim_action_evidence",
        sa.Column("evidence_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.String(160), nullable=False),
        sa.Column("subject_key", sa.String(200), nullable=False),
        sa.Column("action_key", sa.String(80), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.Column("artifact_sha", sa.String(64), nullable=True),
        sa.Column("authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"source_type IN ({_SOURCE_TYPES})", name="ck_claim_evidence_source_type"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_claim_evidence_payload_object"),
        sa.CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="ck_claim_evidence_digest"),
        sa.CheckConstraint("artifact_sha IS NULL OR artifact_sha ~ '^[0-9a-f]{40}$'", name="ck_claim_evidence_sha"),
        sa.CheckConstraint("expires_at IS NULL OR expires_at > observed_at", name="ck_claim_evidence_expiry"),
        sa.CheckConstraint("length(btrim(execution_id)) > 0 AND length(btrim(subject_key)) > 0 AND length(btrim(action_key)) > 0", name="ck_claim_evidence_identity"),
        sa.CheckConstraint("NOT (source_type IN ('agent_self_report','mainai_generated_text','user_attestation','unknown') AND authoritative)", name="ck_claim_evidence_no_self_authority"),
    )
    op.create_index("ix_claim_evidence_binding", "claim_action_evidence", ["owner_id", "execution_id", "subject_key", "action_key"])
    op.create_index("ix_claim_evidence_artifact", "claim_action_evidence", ["artifact_sha"])
    op.create_index("ix_claim_evidence_observed", "claim_action_evidence", ["observed_at"])

    op.create_table(
        "claim_action_receipts",
        sa.Column("receipt_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.String(160), nullable=False),
        sa.Column("subject_key", sa.String(200), nullable=False),
        sa.Column("action_key", sa.String(80), nullable=False),
        sa.Column("declared_action", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("permitted_action", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("executed_action", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("observed_result", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("declared_state", sa.String(32), nullable=False),
        sa.Column("effective_state", sa.String(32), nullable=False),
        sa.Column("action_state", sa.String(24), nullable=False),
        sa.Column("verification_state", sa.String(32), nullable=False),
        sa.Column("evidence_id", sa.UUID(), sa.ForeignKey("claim_action_evidence.evidence_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("predecessor_receipt_id", sa.UUID(), sa.ForeignKey("claim_action_receipts.receipt_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("authority_snapshot", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"declared_state IN ({_CLAIM_STATES})", name="ck_claim_receipt_declared_state"),
        sa.CheckConstraint(f"effective_state IN ({_CLAIM_STATES})", name="ck_claim_receipt_effective_state"),
        sa.CheckConstraint(f"action_state IN ({_ACTION_STATES})", name="ck_claim_receipt_action_state"),
        sa.CheckConstraint(f"verification_state IN ({_VERIFICATION_STATES})", name="ck_claim_receipt_verification_state"),
        sa.CheckConstraint("jsonb_typeof(declared_action)='object' AND jsonb_typeof(permitted_action)='object' AND jsonb_typeof(executed_action)='object' AND jsonb_typeof(observed_result)='object' AND jsonb_typeof(authority_snapshot)='object'", name="ck_claim_receipt_json_objects"),
        sa.CheckConstraint("effective_state IN ('intended','requested','unknown') OR evidence_id IS NOT NULL", name="ck_claim_receipt_upgrade_needs_evidence"),
    )
    op.create_index("ix_claim_receipt_binding", "claim_action_receipts", ["owner_id", "execution_id", "subject_key", "action_key", "created_at"])
    op.create_index("ix_claim_receipt_effective_state", "claim_action_receipts", ["effective_state"])

    for table in ("claim_action_evidence", "claim_action_receipts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_owner_read ON {table}
            FOR SELECT USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON {table} TO mainai_app")

    op.execute("""
    CREATE FUNCTION claim_action_integrity_guard() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
    DECLARE
      v_e public.claim_action_evidence%ROWTYPE;
      v_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
      v_operation uuid := NULLIF(current_setting('app.account_erasure_operation_id', true), '')::uuid;
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION '% is append-only: UPDATE is forbidden', TG_TABLE_NAME;
      END IF;
      IF TG_OP = 'DELETE' THEN
        IF current_setting('app.claim_action_erasure_in_progress', true) <> 'true'
           OR OLD.owner_id IS DISTINCT FROM v_owner
           OR v_operation IS NULL
           OR NOT EXISTS (
             SELECT 1 FROM public.account_erasure_operations op
             WHERE op.operation_id=v_operation AND op.owner_id=v_owner
               AND op.status='active' AND op.phase='personal_data_erasure'
           ) THEN
          RAISE EXCEPTION '% is append-only: DELETE requires governed owner erasure', TG_TABLE_NAME;
        END IF;
        RETURN OLD;
      END IF;
      IF session_user = 'mainai_app' THEN
        RAISE EXCEPTION 'ordinary MainAI runtime cannot create claim/action authority records';
      END IF;
      IF TG_TABLE_NAME = 'claim_action_evidence' THEN
        IF NEW.source_type IN ('agent_self_report','mainai_generated_text','user_attestation','unknown') AND NEW.authoritative THEN
          RAISE EXCEPTION 'self-report, generated text, and attestation cannot be authoritative';
        END IF;
        IF NEW.payload->>'fact_mutability' IS NULL
           OR NEW.payload->>'fact_mutability' NOT IN ('immutable_fact','mutable_snapshot') THEN
          RAISE EXCEPTION 'evidence fact mutability classification is required';
        END IF;
        IF NEW.payload->>'fact_mutability'='mutable_snapshot'
           AND (NEW.expires_at IS NULL OR NEW.expires_at > NEW.observed_at + interval '5 minutes') THEN
          RAISE EXCEPTION 'mutable evidence requires governed freshness';
        END IF;
        IF NEW.payload->>'fact_mutability'='immutable_fact' AND NEW.expires_at IS NOT NULL THEN
          RAISE EXCEPTION 'immutable evidence cannot carry mutable-state expiry';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.effective_state NOT IN ('intended','requested','unknown') THEN
        IF NEW.evidence_id IS NULL THEN RAISE EXCEPTION 'upgraded claim requires evidence'; END IF;
        SELECT * INTO v_e FROM public.claim_action_evidence WHERE evidence_id=NEW.evidence_id;
        IF NOT FOUND OR v_e.owner_id<>NEW.owner_id OR v_e.execution_id<>NEW.execution_id
           OR v_e.subject_key<>NEW.subject_key OR v_e.action_key<>NEW.action_key OR NOT v_e.authoritative THEN
          RAISE EXCEPTION 'claim receipt evidence binding is invalid';
        END IF;
        IF v_e.source_type IN ('agent_self_report','mainai_generated_text','user_attestation','unknown') THEN
          RAISE EXCEPTION 'self-generated material cannot upgrade a claim';
        END IF;
        IF NEW.effective_state='certified' AND v_e.source_type<>'verification_registry' THEN
          RAISE EXCEPTION 'certification requires verification-registry evidence';
        ELSIF NEW.effective_state='merged' AND v_e.source_type<>'github' THEN
          RAISE EXCEPTION 'merge requires GitHub evidence';
        ELSIF NEW.effective_state='deployed' AND v_e.source_type<>'deployment_provider' THEN
          RAISE EXCEPTION 'deployment requires provider evidence';
        ELSIF NEW.effective_state='activated' AND v_e.source_type NOT IN ('deployment_provider','database','external_service') THEN
          RAISE EXCEPTION 'activation requires authoritative external evidence';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("REVOKE ALL ON FUNCTION claim_action_integrity_guard() FROM PUBLIC")
    for table in ("claim_action_evidence", "claim_action_receipts"):
        op.execute(f"""CREATE TRIGGER trg_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION claim_action_integrity_guard()""")

    op.execute("""
    CREATE FUNCTION erase_own_claim_action_integrity_children() RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
    DECLARE
      v_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
      v_operation uuid := NULLIF(current_setting('app.account_erasure_operation_id', true), '')::uuid;
    BEGIN
      IF v_owner IS NULL OR v_operation IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.account_erasure_operations op
        WHERE op.operation_id=v_operation AND op.owner_id=v_owner
          AND op.status='active' AND op.phase='personal_data_erasure'
      ) THEN
        RAISE EXCEPTION 'claim/action erasure requires the governed personal-data phase';
      END IF;
      PERFORM set_config('app.claim_action_erasure_in_progress','true',true);
      DELETE FROM public.claim_action_receipts WHERE owner_id=v_owner;
      DELETE FROM public.claim_action_evidence WHERE owner_id=v_owner;
    END $$;
    """)
    op.execute("REVOKE ALL ON FUNCTION erase_own_claim_action_integrity_children() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION erase_own_claim_action_integrity_children() TO mainai_app")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM claim_action_receipts) OR EXISTS (SELECT 1 FROM claim_action_evidence)
        THEN RAISE EXCEPTION 'cannot downgrade claim/action integrity kernel while audit rows exist'; END IF; END $$""")
    op.execute("DROP FUNCTION IF EXISTS erase_own_claim_action_integrity_children()")
    for table in ("claim_action_receipts", "claim_action_evidence"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS claim_action_integrity_guard()")
    op.drop_table("claim_action_receipts")
    op.drop_table("claim_action_evidence")
