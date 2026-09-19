"""Bind high-risk workforce verification evidence to exact assignments.

Revision ID: 0070
Revises: 0069
"""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE workforce_verification_evidence_bindings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            evidence_id uuid NOT NULL,
            evidence_execution_id uuid NOT NULL,
            capability_key varchar(128) NOT NULL,
            binding_kind varchar(64) NOT NULL DEFAULT 'high_risk_assignment_verification',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_verification_evidence_binding_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_workforce_verification_evidence_binding_assignment UNIQUE (owner_id, assignment_id, evidence_id),
            CONSTRAINT uq_workforce_verification_evidence_single_use UNIQUE (owner_id, evidence_id),
            CONSTRAINT fk_workforce_verification_evidence_binding_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES workforce_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_workforce_verification_evidence_binding_evidence_owner FOREIGN KEY (evidence_id, owner_id)
                REFERENCES intelligence_evidence (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_workforce_verification_evidence_binding_execution_owner FOREIGN KEY (evidence_execution_id, owner_id)
                REFERENCES intelligence_executions (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_workforce_verification_evidence_binding_kind CHECK (
                binding_kind IN ('high_risk_assignment_verification')
            ),
            CONSTRAINT ck_workforce_verification_evidence_binding_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_verification_evidence_binding_assignment
            ON workforce_verification_evidence_bindings(owner_id, assignment_id, created_at);
        CREATE INDEX ix_workforce_verification_evidence_binding_execution
            ON workforce_verification_evidence_bindings(owner_id, evidence_execution_id);

        ALTER TABLE workforce_verification_evidence_bindings ENABLE ROW LEVEL SECURITY;
        ALTER TABLE workforce_verification_evidence_bindings FORCE ROW LEVEL SECURITY;
        CREATE POLICY workforce_verification_evidence_bindings_isolation
            ON workforce_verification_evidence_bindings
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workforce_verification_evidence_bindings CASCADE")
