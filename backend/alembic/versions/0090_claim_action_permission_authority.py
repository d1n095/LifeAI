"""Add the dedicated claim/action permission-authority evidence source.

Revision ID: 0090_claim_permission_authority
Revises: 0089_claim_action_integrity
Create Date: 2026-10-05
"""

from alembic import op


revision = "0090_claim_permission_authority"
down_revision = "0089_claim_action_integrity"
branch_labels = None
depends_on = None

_PREVIOUS = "'github','database','filesystem','ci','test_runner','deployment_provider','task_execution_ledger','verification_registry','external_service','agent_self_report','mainai_generated_text','user_attestation','unknown'"
_CURRENT = "'github','database','filesystem','ci','test_runner','deployment_provider','task_execution_ledger','verification_registry','external_service','permission_authority','agent_self_report','mainai_generated_text','user_attestation','unknown'"


def _replace_source_constraint(values: str) -> None:
    op.drop_constraint("ck_claim_evidence_source_type", "claim_action_evidence", type_="check")
    op.create_check_constraint(
        "ck_claim_evidence_source_type",
        "claim_action_evidence",
        f"source_type IN ({values})",
    )


def upgrade() -> None:
    _replace_source_constraint(_CURRENT)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM claim_action_evidence WHERE source_type='permission_authority') THEN
            RAISE EXCEPTION 'cannot downgrade while permission-authority evidence exists';
          END IF;
        END $$
    """)
    _replace_source_constraint(_PREVIOUS)
