"""Round 11: the Cognitive Gate records MODEL-MODE runs too.

Migration 0075's dataset-version CHECK allowed only 'cognitive_gate_%' — written when
the gate was deterministic-only. Round 11's model-mode runner records runs with
dataset_version='model_gate_r11' (real model behavior trials through the production
path), which the R11 report distinguishes by prefix. The constraint widens to accept
both families; the objective/rubric separation and every other constraint are unchanged.
"""

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE cognitive_gate_runs DROP CONSTRAINT ck_cognitive_gate_runs_dataset_version;
        ALTER TABLE cognitive_gate_runs ADD CONSTRAINT ck_cognitive_gate_runs_dataset_version
            CHECK (dataset_version LIKE 'cognitive_gate_%' OR dataset_version LIKE 'model_gate_%');
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE cognitive_gate_runs DROP CONSTRAINT ck_cognitive_gate_runs_dataset_version;
        ALTER TABLE cognitive_gate_runs ADD CONSTRAINT ck_cognitive_gate_runs_dataset_version
            CHECK (dataset_version LIKE 'cognitive_gate_%');
        DELETE FROM cognitive_gate_runs WHERE dataset_version LIKE 'model_gate_%';
    """)
