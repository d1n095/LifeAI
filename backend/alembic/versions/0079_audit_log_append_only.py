"""audit_log becomes append-only — enforced, not only documented

`AuditLog`'s docstring has called the table an "immutable trail of security-relevant actions" since
it was created, and nothing enforced that: no RLS policy, no privilege restriction, no trigger. Any
connection with UPDATE or DELETE on the table — including a compromised application role, or an
agent that reaches the database through an inherited credential — could rewrite or erase the history
of what it did. An audit trail that the audited party can edit is a log, not a trail.

This migration makes the claim true at the storage layer, where it cannot be bypassed by
application code paths, while keeping the one legitimate mutation a founder has a *right* to:

  * DELETE on `audit_log` raises `restrict_violation`;
  * UPDATE raises unless it is pure ANONYMISATION — the row's `user_id` and/or `ip_address` moved
    from a value to NULL and every security-relevant field (`action`, `entity_type`, `entity_id`,
    `detail`, `created_at`, `id`) is byte-identical. That is what account erasure performs
    (`app/account/erasure.py`: `user_id=None`), and a right-to-erasure that cannot run because the
    audit trail is frozen is not a security property, it is a compliance defect. Re-identification
    (NULL back to a value) and any rewrite of the recorded facts are refused;
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
            -- The single permitted mutation: strip personal identifiers, change nothing else.
            IF TG_OP = 'UPDATE'
               AND NEW.id = OLD.id
               AND NEW.action IS NOT DISTINCT FROM OLD.action
               AND NEW.entity_type IS NOT DISTINCT FROM OLD.entity_type
               AND NEW.entity_id IS NOT DISTINCT FROM OLD.entity_id
               AND NEW.detail IS NOT DISTINCT FROM OLD.detail
               AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
               AND (NEW.user_id IS NULL OR NEW.user_id = OLD.user_id)
               AND (NEW.ip_address IS NULL OR NEW.ip_address = OLD.ip_address)
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'audit_log is append-only (anonymisation is the only permitted change): % refused (row %)',
                TG_OP, COALESCE(OLD.id, NEW.id)
                USING ERRCODE = 'restrict_violation',
                      HINT = 'Append a new audit row instead of changing history; erasure may only null personal identifiers.';
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
