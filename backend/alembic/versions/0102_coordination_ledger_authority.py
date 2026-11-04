"""Serialize owner ledgers and deduplicate legacy events durably.

Revision ID: 0102_coord_ledger_authority
Revises: 0101_coordination_fencing
"""

from alembic import op

revision = "0102_coord_ledger_authority"
down_revision = "0101_coordination_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE coordination_owner_ledgers (
          owner_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
          payload jsonb NOT NULL DEFAULT '{}'::jsonb,
          updated_at timestamp NOT NULL DEFAULT now()
        );
        CREATE TABLE coordination_event_receipts (
          owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          namespace varchar(64) NOT NULL,
          event_id varchar(512) NOT NULL,
          payload_sha256 varchar(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
          recorded_at timestamp NOT NULL DEFAULT now(),
          PRIMARY KEY (owner_id, namespace, event_id)
        );

        CREATE FUNCTION coordination_event_receipts_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          RAISE EXCEPTION 'coordination_event_receipts is append-only';
        END $$;
        REVOKE ALL ON FUNCTION coordination_event_receipts_deny_mutation() FROM PUBLIC;
        CREATE TRIGGER trg_coordination_event_receipts_deny_mutation
          BEFORE UPDATE OR DELETE ON coordination_event_receipts
          FOR EACH ROW EXECUTE FUNCTION coordination_event_receipts_deny_mutation();

        ALTER TABLE coordination_owner_ledgers ENABLE ROW LEVEL SECURITY;
        ALTER TABLE coordination_owner_ledgers FORCE ROW LEVEL SECURITY;
        CREATE POLICY coordination_owner_ledgers_owner ON coordination_owner_ledgers
          USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        ALTER TABLE coordination_event_receipts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE coordination_event_receipts FORCE ROW LEVEL SECURITY;
        CREATE POLICY coordination_event_receipts_owner ON coordination_event_receipts
          USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        REVOKE ALL ON coordination_owner_ledgers, coordination_event_receipts FROM PUBLIC;
        GRANT SELECT, INSERT, UPDATE ON coordination_owner_ledgers TO mainai_app;
        GRANT SELECT, INSERT ON coordination_event_receipts TO mainai_app;

        CREATE UNIQUE INDEX uq_workforce_assignment_active_request
          ON workforce_assignments(owner_id, delegation_request_id)
          WHERE status NOT IN ('failed', 'superseded', 'revoked', 'cancelled');

        CREATE FUNCTION enforce_workforce_assignment_status_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF NEW.status = OLD.status THEN RETURN NEW; END IF;
          IF NOT (
            (OLD.status = 'assigned' AND NEW.status IN ('running','awaiting_verification','completed','failed','revoked','superseded')) OR
            (OLD.status = 'running' AND NEW.status IN ('awaiting_verification','failed','revoked','superseded')) OR
            (OLD.status = 'awaiting_verification' AND NEW.status IN ('completed','failed','revoked','superseded')) OR
            (OLD.status = 'failed' AND NEW.status IN ('assigned','revoked','superseded'))
          ) THEN
            RAISE EXCEPTION 'invalid workforce assignment status transition: % -> %', OLD.status, NEW.status
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$;
        REVOKE ALL ON FUNCTION enforce_workforce_assignment_status_transition() FROM PUBLIC;
        CREATE TRIGGER trg_workforce_assignment_status_transition
          BEFORE UPDATE OF status ON workforce_assignments
          FOR EACH ROW EXECUTE FUNCTION enforce_workforce_assignment_status_transition();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER trg_workforce_assignment_status_transition ON workforce_assignments;
        DROP FUNCTION enforce_workforce_assignment_status_transition();
        DROP INDEX uq_workforce_assignment_active_request;
        DROP TABLE coordination_event_receipts;
        DROP FUNCTION coordination_event_receipts_deny_mutation();
        DROP TABLE coordination_owner_ledgers;
        """
    )
