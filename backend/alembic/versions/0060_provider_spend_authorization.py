"""Provider-spend authorization -- distinct founder-granted budget for billed planning calls.

WHY THIS EXISTS (Autonomy Activation Lane / FIRST_AUTONOMOUS_TASK_BLOCKER_MAP B1):

`app.development_supervisor.production_entry` reconstructs `SupervisorScope` with
`provider_spend_authorized=False` hardcoded. That is correct fail-closed behavior today: a
bare execution envelope authorizes *local repo work under paths/capabilities/risk*, not
spending money on a provider. Without a hand-built `PlanCandidate` or a gap-derived
deterministic recipe, every ordinary production task therefore stops at
`PROVIDER_SPEND_NOT_AUTHORIZED`.

This migration does NOT flip that boolean. It creates the missing *foundation* so a founder
can grant a bounded, revocable, accountably spent provider-planning budget for a specific
(owner, goal, execution envelope). The final one-line wire into `production_entry.py` is
deliberately deferred (Claude currently owns adjacent Supervisor/worker authority surfaces).

Hard rules, structural:
  - REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY. This table never grants paths,
    capabilities, remote_write, or push. It only answers: may this goal spend up to these
    ceilings calling an allowlisted provider for planning?
  - Grant cites the exact `execution_authorization_envelopes` row it was issued under
    (composite owner-anchored FK). If that envelope is no longer the goal's current active
    envelope, `get_current_provider_spend_authorization()` returns None -- fail closed, never
    silently inherit spend under a different authority.
  - Retry never invents budget: usage events are append-only and idempotent on `source_ref`.
  - Concurrent workers cannot double-spend: reservation takes `FOR UPDATE` on the
    authorization row and checks remaining ceilings before recording usage.
  - Exhausted / expired / revoked / superseded => unavailable. No wake-up that reopens spend
    without a new founder grant.

Column-specific `ON DELETE SET NULL (supersedes_authorization_id)` -- same lesson as
migration 0058/0059: a plain composite SET NULL would also null NOT NULL `owner_id`.
"""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE provider_spend_authorizations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            execution_envelope_id uuid NOT NULL,
            authorized_by varchar(64) NOT NULL,
            authorized_at timestamp NOT NULL DEFAULT now(),
            status varchar(24) NOT NULL DEFAULT 'active',
            max_cost_usd numeric(14, 6) NOT NULL,
            max_requests integer NOT NULL,
            max_prompt_tokens integer,
            max_completion_tokens integer,
            allowed_providers jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb,
            expires_at timestamp,
            spent_cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
            spent_requests integer NOT NULL DEFAULT 0,
            spent_prompt_tokens integer NOT NULL DEFAULT 0,
            spent_completion_tokens integer NOT NULL DEFAULT 0,
            supersedes_authorization_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_spend_authorizations_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_provider_spend_authorizations_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_provider_spend_authorizations_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_provider_spend_authorizations_envelope_owner FOREIGN KEY (execution_envelope_id, owner_id)
                REFERENCES execution_authorization_envelopes (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_provider_spend_authorizations_supersedes FOREIGN KEY (supersedes_authorization_id, owner_id)
                REFERENCES provider_spend_authorizations (id, owner_id)
                ON DELETE SET NULL (supersedes_authorization_id),
            CONSTRAINT ck_provider_spend_authorizations_status CHECK (status IN (
                'active', 'superseded', 'exhausted', 'expired', 'revoked'
            )),
            CONSTRAINT ck_provider_spend_authorizations_ceilings CHECK (
                max_cost_usd >= 0 AND max_requests >= 0
                AND spent_cost_usd >= 0 AND spent_requests >= 0
                AND spent_prompt_tokens >= 0 AND spent_completion_tokens >= 0
                AND (max_prompt_tokens IS NULL OR max_prompt_tokens >= 0)
                AND (max_completion_tokens IS NULL OR max_completion_tokens >= 0)
            ),
            CONSTRAINT ck_provider_spend_authorizations_providers CHECK (jsonb_typeof(allowed_providers) = 'array'),
            CONSTRAINT ck_provider_spend_authorizations_models CHECK (jsonb_typeof(allowed_models) = 'array'),
            CONSTRAINT ck_provider_spend_authorizations_provenance CHECK (jsonb_typeof(provenance) = 'object'),
            CONSTRAINT ck_provider_spend_authorizations_no_self_supersede CHECK (
                supersedes_authorization_id IS NULL OR supersedes_authorization_id <> id
            )
        );
        CREATE INDEX ix_provider_spend_authorizations_owner_goal_status
            ON provider_spend_authorizations(owner_id, goal_id, status);
        CREATE INDEX ix_provider_spend_authorizations_envelope
            ON provider_spend_authorizations(execution_envelope_id);

        ALTER TABLE provider_spend_authorizations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE provider_spend_authorizations FORCE ROW LEVEL SECURITY;
        CREATE POLICY provider_spend_authorizations_isolation ON provider_spend_authorizations
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE TABLE provider_spend_usage_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            authorization_id uuid NOT NULL,
            goal_id uuid NOT NULL,
            task_id uuid,
            job_id uuid,
            provider varchar(64) NOT NULL,
            model varchar(128) NOT NULL,
            prompt_tokens integer NOT NULL DEFAULT 0,
            completion_tokens integer NOT NULL DEFAULT 0,
            cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_ref varchar(320) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_spend_usage_events_source_ref UNIQUE (source_ref),
            CONSTRAINT fk_provider_spend_usage_events_auth_owner FOREIGN KEY (authorization_id, owner_id)
                REFERENCES provider_spend_authorizations (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_provider_spend_usage_events_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_provider_spend_usage_events_nonneg CHECK (
                prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_usd >= 0
            ),
            CONSTRAINT ck_provider_spend_usage_events_evidence CHECK (jsonb_typeof(evidence) = 'object')
        );
        CREATE INDEX ix_provider_spend_usage_events_auth
            ON provider_spend_usage_events(authorization_id, observed_at);

        ALTER TABLE provider_spend_usage_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE provider_spend_usage_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY provider_spend_usage_events_isolation ON provider_spend_usage_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        -- Append-only usage: UPDATE denied; DELETE only via authorized erasure GUC.
        CREATE FUNCTION provider_spend_usage_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.provider_spend_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'provider_spend_usage_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'provider_spend_usage_events is append-only: UPDATE is never permitted.';
        END;
        $$;
        CREATE TRIGGER provider_spend_usage_events_deny_mutation
            BEFORE UPDATE OR DELETE ON provider_spend_usage_events
            FOR EACH ROW EXECUTE FUNCTION provider_spend_usage_events_deny_mutation();
        REVOKE ALL ON FUNCTION provider_spend_usage_events_deny_mutation() FROM PUBLIC;

        CREATE FUNCTION erase_own_provider_spend_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_provider_spend_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.provider_spend_erasure_in_progress', 'on', true);
            DELETE FROM public.provider_spend_usage_events WHERE owner_id = v_owner_id;
            DELETE FROM public.provider_spend_authorizations WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_provider_spend_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS erase_own_provider_spend_children();
        DROP FUNCTION IF EXISTS provider_spend_usage_events_deny_mutation();
        DROP TABLE IF EXISTS provider_spend_usage_events;
        DROP TABLE IF EXISTS provider_spend_authorizations;
    """)
