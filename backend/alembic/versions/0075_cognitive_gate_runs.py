"""Round 10 M3: the COGNITIVE GATE's durable run records.

The Cognitive Regression Gate (app/mainai_evals/cognitive_gate.py) executes fixed,
versioned behavioral cases through production call paths and compares runs across model
versions. This migration gives those runs a queryable home so a candidate model's gate
results are EVIDENCE on record, not a console transcript:

  - cognitive_gate_runs: one row per executed gate run — dataset version, provider+model
    it ran against, and the objective verdict (all critical objective assertions passed).
  - cognitive_gate_case_results: one row per case per run. Deterministic objective
    assertions and model-judged rubric scores live in SEPARATE column sets
    (objective_json vs rubric_json) — the storage layer itself preserves the separation
    between deterministic failures (auto-enforced) and subjective quality (recorded,
    reviewed by humans, never auto-enforced).

Not RLS-protected, like provider_config/provider_verification_checks (migration 0013's
precedent): gate runs are founder-wide configuration-state evidence, not per-owner data.

This table RECORDS verdicts; it does not ENFORCE them. No model switching, no deployment
decision, no privileged change is wired here — the authorized operational system owns
that boundary (see the gate module's docstring).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cognitive_gate_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("executed_at", sa.DateTime, nullable=False, index=True),
        sa.Column("objective_passed", sa.Boolean, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "dataset_version LIKE 'cognitive_gate_%'",
            name="ck_cognitive_gate_runs_dataset_version",
        ),
    )
    op.create_table(
        "cognitive_gate_case_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cognitive_gate_runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("execution", sa.String(16), nullable=False),  # deterministic | model
        sa.Column("status", sa.String(24), nullable=False),  # passed | failed | skipped_no_model
        sa.Column("objective_json", postgresql.JSONB, nullable=True),
        sa.Column("rubric_json", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "execution IN ('deterministic', 'model')", name="ck_cognitive_gate_case_execution"
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'skipped_no_model')",
            name="ck_cognitive_gate_case_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("cognitive_gate_case_results")
    op.drop_table("cognitive_gate_runs")
