"""High-risk workforce verification evidence claim fence.

Revision ID: 0082_high_risk_evidence_claim
Revises: 0081_recall_prod_activation
Create Date: 2026-09-20
"""

from alembic import op

revision = "0082_high_risk_evidence_claim"
down_revision = "0081_recall_prod_activation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_workforce_verified_test_evidence_ref
        ON workforce_verification_decisions(owner_id, test_evidence_ref)
        WHERE decision = 'VERIFIED'
          AND test_evidence_ref IS NOT NULL
          AND policy_snapshot->>'risk' = 'high'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_workforce_verified_test_evidence_ref")
