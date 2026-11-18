"""audit_log becomes append-only — enforced, not only documented

`AuditLog`'s docstring has called the table an "immutable trail of security-relevant actions" since
it was created, and nothing enforced that: no RLS policy, no privilege restriction, no trigger. Any
connection with UPDATE or DELETE on the table — including a compromised application role, or an
agent that reaches the database through an inherited credential — could rewrite or erase the history
of what it did. An audit trail that the audited party can edit is a log, not a trail.

This migration makes the claim true at the storage layer, where it cannot be bypassed by
application code paths:

  * UPDATE and DELETE on `audit_log` raise `restrict_violation` with an explanatory message;
  * TRUNCATE is left alone deliberately — it is a maintenance/test operation that already requires
    table ownership, it is not reachable from application code, and blocking it would break the
    suite's own table cleanup without protecting anything a row trigger protects.

Revision ID: 0079
Revises: 0078
"""
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only: % is not permitted (row %)', TG_OP, COALESCE(OLD.id, NEW.id)
                USING ERRCODE = 'restrict_violation',
                      HINT = 'Record a new audit row instead of changing history.';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_mutation()")
