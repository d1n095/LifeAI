"""High-risk workforce verification evidence claim fence.

Revision ID: 0070
Revises: 0069
Create Date: 2026-09-19
"""

from alembic import op

revision = "0070"
down_revision = "0069"
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
