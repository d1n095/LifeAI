"""P3 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.1, §8 build order): the first slice of
the shared MainAI Memory Core — classifies WHAT a claim already extracted by STEG 10 actually
is (idea/decision/task_reference/vision/technical/historical/uncategorized), extracted in the
SAME AI call as the claim text itself (see app/rag/claims.py). This is the input P4's later
interpretation queue will read to decide whether a claim becomes a project_entities row — no
project_entities/interpretation_proposals/founder_memory_notes tables yet, this migration is
additive-only and touches nothing else.

Same varchar-not-native-enum pattern as knowledge_claims.status/confidence (migration 0007) —
a new ClaimType value never needs its own migration.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-28
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_claims
            ADD COLUMN claim_type varchar(32) NOT NULL DEFAULT 'uncategorized';
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_claims
            DROP COLUMN IF EXISTS claim_type;
    """)
