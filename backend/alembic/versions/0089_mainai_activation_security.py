"""MainAI activation authority substrate.

Revision ID: 0089_mainai_activation_security
Revises: 0088_erasure_completion_phase
"""

from alembic import op

revision = "0089_mainai_activation_security"
down_revision = "0088_erasure_completion_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE execution_authorization_envelopes
          ADD COLUMN base_branch text,
          ADD COLUMN approved_base_sha char(40),
          ADD COLUMN required_check_contexts jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN authority_version integer NOT NULL DEFAULT 1,
          ADD CONSTRAINT ck_execution_envelopes_activation_sha CHECK (
            approved_base_sha IS NULL OR approved_base_sha ~ '^[0-9a-f]{40}$'),
          ADD CONSTRAINT ck_execution_envelopes_required_checks CHECK (
            jsonb_typeof(required_check_contexts) = 'array'),
          ADD CONSTRAINT ck_execution_envelopes_authority_version CHECK (authority_version > 0);

        CREATE FUNCTION restrict_activation_envelope_bindings() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.repository_identity IS DISTINCT FROM OLD.repository_identity
             OR NEW.base_branch IS DISTINCT FROM OLD.base_branch
             OR NEW.approved_base_sha IS DISTINCT FROM OLD.approved_base_sha
             OR NEW.required_check_contexts IS DISTINCT FROM OLD.required_check_contexts
             OR NEW.authority_version IS DISTINCT FROM OLD.authority_version THEN
            RAISE EXCEPTION 'activation envelope authority bindings are immutable; supersede the envelope';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER activation_envelope_bindings_immutable
          BEFORE UPDATE ON execution_authorization_envelopes
          FOR EACH ROW EXECUTE FUNCTION restrict_activation_envelope_bindings();

        CREATE TABLE mainai_activation_preflight_receipts (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_id uuid NOT NULL REFERENCES mainai_goals(id) ON DELETE CASCADE,
          execution_envelope_id uuid NOT NULL,
          provider_spend_authorization_id uuid NOT NULL,
          repository_identity text NOT NULL,
          integration_branch text NOT NULL,
          activation_sha char(40) NOT NULL,
          base_branch text NOT NULL,
          base_sha char(40) NOT NULL,
          authority_version integer NOT NULL,
          required_check_contexts jsonb NOT NULL,
          runtime_config_digest char(64) NOT NULL,
          runtime_token_identity text NOT NULL,
          runtime_token_attestation_digest char(64) NOT NULL,
          ruleset_attestation_digest char(64) NOT NULL,
          founder_attestation_digest char(64) NOT NULL,
          facts jsonb NOT NULL,
          unknown_required_facts jsonb NOT NULL,
          verdict varchar(16) NOT NULL,
          observed_at timestamp NOT NULL DEFAULT now(),
          expires_at timestamp NOT NULL,
          invalidated_at timestamp,
          receipt_digest char(64) NOT NULL,
          provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamp NOT NULL DEFAULT now(),
          CONSTRAINT uq_mainai_activation_preflight_digest UNIQUE(receipt_digest),
          CONSTRAINT uq_mainai_activation_preflight_id_owner UNIQUE(id, owner_id),
          CONSTRAINT fk_activation_preflight_envelope_owner FOREIGN KEY(execution_envelope_id, owner_id)
            REFERENCES execution_authorization_envelopes(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_activation_preflight_spend_owner FOREIGN KEY(provider_spend_authorization_id, owner_id)
            REFERENCES provider_spend_authorizations(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT ck_activation_preflight_sha CHECK (
            activation_sha ~ '^[0-9a-f]{40}$' AND base_sha ~ '^[0-9a-f]{40}$'),
          CONSTRAINT ck_activation_preflight_json CHECK (
            jsonb_typeof(required_check_contexts)='array' AND jsonb_typeof(facts)='object'
            AND jsonb_typeof(unknown_required_facts)='array' AND jsonb_typeof(provenance)='object'),
          CONSTRAINT ck_activation_preflight_verdict CHECK(verdict IN ('go','no_go')),
          CONSTRAINT ck_activation_preflight_unknowns CHECK(
            verdict='no_go' OR jsonb_array_length(unknown_required_facts)=0),
          CONSTRAINT ck_activation_preflight_expiry CHECK(expires_at > observed_at),
          CONSTRAINT ck_activation_preflight_authority_version CHECK(authority_version > 0)
        );
        CREATE INDEX ix_activation_preflight_owner_goal ON mainai_activation_preflight_receipts(owner_id, goal_id, created_at);
        ALTER TABLE mainai_activation_preflight_receipts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_activation_preflight_receipts FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_activation_preflight_isolation ON mainai_activation_preflight_receipts
          USING(owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK(owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE TABLE mainai_push_grants (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_id uuid NOT NULL REFERENCES mainai_goals(id) ON DELETE CASCADE,
          task_id uuid NOT NULL REFERENCES mainai_tasks(id) ON DELETE CASCADE,
          job_id uuid NOT NULL REFERENCES mainai_jobs(id) ON DELETE CASCADE,
          worktree_id uuid NOT NULL REFERENCES mainai_task_worktrees(id) ON DELETE CASCADE,
          execution_envelope_id uuid NOT NULL,
          preflight_receipt_id uuid NOT NULL,
          repository_identity text NOT NULL,
          branch text NOT NULL,
          base_branch text NOT NULL,
          base_sha char(40) NOT NULL,
          local_commit_sha char(40) NOT NULL,
          authority_version integer NOT NULL,
          authorized_by varchar(64) NOT NULL,
          authorized_at timestamp NOT NULL DEFAULT now(),
          expires_at timestamp NOT NULL,
          status varchar(24) NOT NULL DEFAULT 'active',
          consumed_at timestamp,
          consumed_by_job_id uuid REFERENCES mainai_jobs(id) ON DELETE SET NULL,
          idempotency_key varchar(128) NOT NULL,
          provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamp NOT NULL DEFAULT now(),
          CONSTRAINT uq_mainai_push_grants_owner_idem UNIQUE(owner_id, idempotency_key),
          CONSTRAINT fk_push_grant_envelope_owner FOREIGN KEY(execution_envelope_id, owner_id)
            REFERENCES execution_authorization_envelopes(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT fk_push_grant_receipt_owner FOREIGN KEY(preflight_receipt_id, owner_id)
            REFERENCES mainai_activation_preflight_receipts(id, owner_id) ON DELETE CASCADE,
          CONSTRAINT ck_push_grant_sha CHECK(base_sha ~ '^[0-9a-f]{40}$' AND local_commit_sha ~ '^[0-9a-f]{40}$'),
          CONSTRAINT ck_push_grant_status CHECK(status IN ('active','consumed','expired','cancelled','superseded')),
          CONSTRAINT ck_push_grant_consumption CHECK(
            (status='consumed' AND consumed_at IS NOT NULL AND consumed_by_job_id IS NOT NULL)
            OR (status<>'consumed' AND consumed_at IS NULL AND consumed_by_job_id IS NULL)),
          CONSTRAINT ck_push_grant_expiry CHECK(expires_at > authorized_at),
          CONSTRAINT ck_push_grant_authority_version CHECK(authority_version > 0),
          CONSTRAINT ck_push_grant_provenance CHECK(jsonb_typeof(provenance)='object')
        );
        CREATE UNIQUE INDEX uq_mainai_push_grants_one_active_task ON mainai_push_grants(owner_id, task_id) WHERE status='active';
        CREATE INDEX ix_mainai_push_grants_job ON mainai_push_grants(job_id);
        ALTER TABLE mainai_push_grants ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_push_grants FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_push_grants_isolation ON mainai_push_grants
          USING(owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
          WITH CHECK(owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE FUNCTION deny_activation_preflight_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'activation preflight receipts are immutable'; END IF;
          IF OLD.invalidated_at IS NULL AND NEW.invalidated_at IS NOT NULL
             AND (to_jsonb(NEW) - 'invalidated_at') = (to_jsonb(OLD) - 'invalidated_at') THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'activation preflight receipts are immutable except one-way invalidation';
        END $$;
        CREATE TRIGGER activation_preflight_immutable BEFORE UPDATE OR DELETE ON mainai_activation_preflight_receipts
          FOR EACH ROW EXECUTE FUNCTION deny_activation_preflight_mutation();

        CREATE FUNCTION restrict_push_grant_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'push grants are auditable and cannot be deleted'; END IF;
          IF OLD.status <> 'active' THEN
            RAISE EXCEPTION 'terminal push grant state is immutable';
          END IF;
          IF NEW.status NOT IN ('consumed','expired','cancelled','superseded') THEN
            RAISE EXCEPTION 'push grant transition must leave active state monotonically';
          END IF;
          IF (to_jsonb(NEW) - ARRAY['status','consumed_at','consumed_by_job_id']) =
             (to_jsonb(OLD) - ARRAY['status','consumed_at','consumed_by_job_id']) THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'push grant authority bindings are immutable';
        END $$;
        CREATE TRIGGER push_grant_bindings_immutable BEFORE UPDATE OR DELETE ON mainai_push_grants
          FOR EACH ROW EXECUTE FUNCTION restrict_push_grant_mutation();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS push_grant_bindings_immutable ON mainai_push_grants;
        DROP FUNCTION IF EXISTS restrict_push_grant_mutation();
        DROP TRIGGER IF EXISTS activation_preflight_immutable ON mainai_activation_preflight_receipts;
        DROP FUNCTION IF EXISTS deny_activation_preflight_mutation();
        DROP TRIGGER IF EXISTS activation_envelope_bindings_immutable ON execution_authorization_envelopes;
        DROP FUNCTION IF EXISTS restrict_activation_envelope_bindings();
        DROP TABLE mainai_push_grants;
        DROP TABLE mainai_activation_preflight_receipts;
        ALTER TABLE execution_authorization_envelopes
          DROP COLUMN authority_version,
          DROP COLUMN required_check_contexts,
          DROP COLUMN approved_base_sha,
          DROP COLUMN base_branch;
    """)
