"""Persistent authorized operating cycles and provider webhook receipts.

Revision ID: 0100_rt3_cycle_webhooks
Revises: 0089_activation_branch_protect
"""

from alembic import op

revision = "0100_rt3_cycle_webhooks"
down_revision = "0089_activation_branch_protect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE authorized_operating_cycles (
          id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_id uuid NOT NULL, execution_envelope_id uuid NOT NULL, verifier_profile_id uuid NOT NULL,
          current_task_id uuid NULL, workforce_assignment_id uuid NULL,
          provider varchar(32) NOT NULL DEFAULT 'cursor', external_agent_id varchar(256) NULL,
          state varchar(32) NOT NULL DEFAULT 'pending', attempt_count integer NOT NULL DEFAULT 0,
          max_attempts integer NOT NULL DEFAULT 3, next_attempt_at timestamp NULL,
          locked_by varchar(128) NULL, lease_generation integer NOT NULL DEFAULT 0,
          lease_expires_at timestamp NULL, authority_epoch integer NOT NULL, global_authority_epoch integer NOT NULL,
          candidate_sha varchar(64) NULL, last_error_code varchar(64) NULL,
          provenance jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamp NOT NULL DEFAULT now(),
          updated_at timestamp NOT NULL DEFAULT now(), completed_at timestamp NULL,
          CONSTRAINT uq_authorized_cycle_id_owner UNIQUE (id, owner_id),
          CONSTRAINT fk_rt3_goal_owner FOREIGN KEY (goal_id, owner_id) REFERENCES mainai_goals(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_rt3_envelope_owner FOREIGN KEY (execution_envelope_id, owner_id) REFERENCES execution_authorization_envelopes(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_rt3_task_owner FOREIGN KEY (current_task_id, owner_id) REFERENCES mainai_tasks(id, owner_id) ON DELETE SET NULL,
          CONSTRAINT fk_rt3_assignment_owner FOREIGN KEY (workforce_assignment_id, owner_id) REFERENCES workforce_assignments(id, owner_id) ON DELETE SET NULL,
          CONSTRAINT uq_authorized_cycle_envelope UNIQUE (owner_id, goal_id, execution_envelope_id),
          CONSTRAINT uq_authorized_cycle_provider_agent UNIQUE (provider, external_agent_id),
          CONSTRAINT ck_rt3_state CHECK (state IN ('pending','delegating','awaiting_dispatch','awaiting_provider','awaiting_verification','verified','blocked','failed','cancelled')),
          CONSTRAINT ck_rt3_attempts CHECK (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10),
          CONSTRAINT ck_rt3_candidate_sha CHECK (candidate_sha IS NULL OR candidate_sha ~ '^[0-9a-f]{40}$')
        );
        CREATE INDEX ix_rt3_claim ON authorized_operating_cycles(state, next_attempt_at, lease_expires_at);
        CREATE INDEX ix_rt3_external_agent ON authorized_operating_cycles(provider, external_agent_id);
        CREATE UNIQUE INDEX uq_rt3_active_task ON authorized_operating_cycles(owner_id, current_task_id)
          WHERE current_task_id IS NOT NULL AND state NOT IN ('verified','failed','cancelled');

        CREATE TABLE provider_webhook_events (
          id uuid PRIMARY KEY, provider varchar(32) NOT NULL, event_key varchar(128) NOT NULL,
          body_sha256 varchar(64) NOT NULL, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          cycle_id uuid NOT NULL, assignment_id uuid NOT NULL, external_agent_id varchar(256) NOT NULL,
          provider_status varchar(32) NOT NULL, occurred_at timestamp NOT NULL,
          received_at timestamp NOT NULL DEFAULT now(), processing_state varchar(32) NOT NULL DEFAULT 'received',
          outcome varchar(64) NULL, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
          CONSTRAINT uq_provider_webhook_event_key UNIQUE (provider, event_key),
          CONSTRAINT fk_webhook_cycle_owner FOREIGN KEY (cycle_id, owner_id) REFERENCES authorized_operating_cycles(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_webhook_assignment_owner FOREIGN KEY (assignment_id, owner_id) REFERENCES workforce_assignments(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT ck_webhook_processing_state CHECK (processing_state IN ('received','applied','rejected','unknown'))
        );
        CREATE INDEX ix_webhook_owner_received ON provider_webhook_events(owner_id, received_at);

        CREATE FUNCTION provider_webhook_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          RAISE EXCEPTION 'provider_webhook_events is append-only';
        END $$;
        REVOKE ALL ON FUNCTION provider_webhook_events_deny_mutation() FROM PUBLIC;
        CREATE TRIGGER trg_provider_webhook_events_deny_mutation
          BEFORE UPDATE OR DELETE ON provider_webhook_events
          FOR EACH ROW EXECUTE FUNCTION provider_webhook_events_deny_mutation();

        ALTER TABLE authorized_operating_cycles ENABLE ROW LEVEL SECURITY;
        ALTER TABLE authorized_operating_cycles FORCE ROW LEVEL SECURITY;
        CREATE POLICY authorized_operating_cycles_owner ON authorized_operating_cycles
          USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        ALTER TABLE provider_webhook_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE provider_webhook_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY provider_webhook_events_owner ON provider_webhook_events
          USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        REVOKE ALL ON authorized_operating_cycles, provider_webhook_events FROM PUBLIC;
        GRANT SELECT, INSERT, UPDATE ON authorized_operating_cycles TO mainai_app;
        GRANT SELECT, INSERT ON provider_webhook_events TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE provider_webhook_events; "
        "DROP FUNCTION provider_webhook_events_deny_mutation(); "
        "DROP TABLE authorized_operating_cycles;"
    )
