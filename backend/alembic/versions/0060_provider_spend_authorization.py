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
  - REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY.
  - At most ONE active spend grant per (owner_id, goal_id) -- partial unique index.
  - Usage idempotency is owner-scoped: UNIQUE (owner_id, source_ref).
  - Real calls use reserve → invoke → settle/release. reserved_* counts against ceilings
    before the provider is contacted; settle adjusts to actuals; release frees the hold.
  - Settle/release UPDATEs go only through SECURITY DEFINER functions (UPDATE revoked from
    mainai_app; append-only trigger opens only under settle GUC set inside those functions).
  - Column-specific ON DELETE SET NULL (supersedes_authorization_id).
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
            max_cost_per_request_usd numeric(14, 6),
            allowed_providers jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb,
            expires_at timestamp,
            spent_cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
            spent_requests integer NOT NULL DEFAULT 0,
            spent_prompt_tokens integer NOT NULL DEFAULT 0,
            spent_completion_tokens integer NOT NULL DEFAULT 0,
            reserved_cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
            reserved_requests integer NOT NULL DEFAULT 0,
            reserved_prompt_tokens integer NOT NULL DEFAULT 0,
            reserved_completion_tokens integer NOT NULL DEFAULT 0,
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
                AND reserved_cost_usd >= 0 AND reserved_requests >= 0
                AND reserved_prompt_tokens >= 0 AND reserved_completion_tokens >= 0
                AND (max_prompt_tokens IS NULL OR max_prompt_tokens >= 0)
                AND (max_completion_tokens IS NULL OR max_completion_tokens >= 0)
                AND (max_cost_per_request_usd IS NULL OR max_cost_per_request_usd >= 0)
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
        CREATE UNIQUE INDEX uq_provider_spend_one_active_per_owner_goal
            ON provider_spend_authorizations(owner_id, goal_id) WHERE status = 'active';

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
            reserved_prompt_tokens integer NOT NULL DEFAULT 0,
            reserved_completion_tokens integer NOT NULL DEFAULT 0,
            reserved_cost_usd numeric(14, 6) NOT NULL DEFAULT 0,
            status varchar(16) NOT NULL DEFAULT 'settled',
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_ref varchar(320) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_spend_usage_events_owner_source_ref UNIQUE (owner_id, source_ref),
            CONSTRAINT fk_provider_spend_usage_events_auth_owner FOREIGN KEY (authorization_id, owner_id)
                REFERENCES provider_spend_authorizations (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_provider_spend_usage_events_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_provider_spend_usage_events_nonneg CHECK (
                prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_usd >= 0
                AND reserved_prompt_tokens >= 0 AND reserved_completion_tokens >= 0
                AND reserved_cost_usd >= 0
            ),
            CONSTRAINT ck_provider_spend_usage_events_status CHECK (status IN (
                'reserved', 'settled', 'released'
            )),
            CONSTRAINT ck_provider_spend_usage_events_evidence CHECK (jsonb_typeof(evidence) = 'object')
        );
        CREATE INDEX ix_provider_spend_usage_events_auth
            ON provider_spend_usage_events(authorization_id, observed_at);

        ALTER TABLE provider_spend_usage_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE provider_spend_usage_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY provider_spend_usage_events_isolation ON provider_spend_usage_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE FUNCTION provider_spend_usage_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.provider_spend_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'provider_spend_usage_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            IF current_setting('app.provider_spend_settle_in_progress', true) = 'on' THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'provider_spend_usage_events is append-only: UPDATE is only permitted through settle/release SECURITY DEFINER paths.';
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

        CREATE FUNCTION settle_provider_spend_usage(
            p_owner_id uuid,
            p_source_ref varchar,
            p_prompt_tokens integer,
            p_completion_tokens integer,
            p_cost_usd numeric,
            p_evidence jsonb DEFAULT '{}'::jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_event public.provider_spend_usage_events%ROWTYPE;
            v_auth public.provider_spend_authorizations%ROWTYPE;
        BEGIN
            IF p_prompt_tokens < 0 OR p_completion_tokens < 0 OR p_cost_usd < 0 THEN
                RAISE EXCEPTION 'settle amounts must be non-negative';
            END IF;
            SELECT * INTO v_event FROM public.provider_spend_usage_events
                WHERE owner_id = p_owner_id AND source_ref = p_source_ref FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider spend usage event missing for settle';
            END IF;
            IF v_event.status = 'settled' THEN
                RETURN v_event.id;
            END IF;
            IF v_event.status <> 'reserved' THEN
                RAISE EXCEPTION 'provider spend usage event is %, cannot settle', v_event.status;
            END IF;
            SELECT * INTO v_auth FROM public.provider_spend_authorizations
                WHERE id = v_event.authorization_id AND owner_id = p_owner_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider spend authorization missing for settle';
            END IF;
            IF p_prompt_tokens > v_event.reserved_prompt_tokens
               OR p_completion_tokens > v_event.reserved_completion_tokens
               OR p_cost_usd > v_event.reserved_cost_usd THEN
                RAISE EXCEPTION 'settle actuals exceed reservation hold';
            END IF;
            PERFORM set_config('app.provider_spend_settle_in_progress', 'on', true);
            UPDATE public.provider_spend_usage_events SET
                status = 'settled',
                prompt_tokens = p_prompt_tokens,
                completion_tokens = p_completion_tokens,
                cost_usd = p_cost_usd,
                evidence = COALESCE(p_evidence, '{}'::jsonb),
                observed_at = now()
            WHERE id = v_event.id;
            UPDATE public.provider_spend_authorizations SET
                reserved_requests = reserved_requests - 1,
                reserved_prompt_tokens = reserved_prompt_tokens - v_event.reserved_prompt_tokens,
                reserved_completion_tokens = reserved_completion_tokens - v_event.reserved_completion_tokens,
                reserved_cost_usd = reserved_cost_usd - v_event.reserved_cost_usd,
                spent_requests = spent_requests + 1,
                spent_prompt_tokens = spent_prompt_tokens + p_prompt_tokens,
                spent_completion_tokens = spent_completion_tokens + p_completion_tokens,
                spent_cost_usd = spent_cost_usd + p_cost_usd
            WHERE id = v_auth.id;
            RETURN v_event.id;
        END;
        $$;
        REVOKE ALL ON FUNCTION settle_provider_spend_usage(uuid, varchar, integer, integer, numeric, jsonb) FROM PUBLIC;

        CREATE FUNCTION release_provider_spend_usage(
            p_owner_id uuid,
            p_source_ref varchar,
            p_evidence jsonb DEFAULT '{}'::jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_event public.provider_spend_usage_events%ROWTYPE;
        BEGIN
            SELECT * INTO v_event FROM public.provider_spend_usage_events
                WHERE owner_id = p_owner_id AND source_ref = p_source_ref FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider spend usage event missing for release';
            END IF;
            IF v_event.status = 'released' THEN
                RETURN v_event.id;
            END IF;
            IF v_event.status = 'settled' THEN
                RAISE EXCEPTION 'cannot release an already-settled provider spend usage event';
            END IF;
            IF v_event.status <> 'reserved' THEN
                RAISE EXCEPTION 'provider spend usage event is %, cannot release', v_event.status;
            END IF;
            PERFORM set_config('app.provider_spend_settle_in_progress', 'on', true);
            UPDATE public.provider_spend_usage_events SET
                status = 'released',
                evidence = COALESCE(p_evidence, '{}'::jsonb),
                observed_at = now()
            WHERE id = v_event.id;
            UPDATE public.provider_spend_authorizations SET
                reserved_requests = reserved_requests - 1,
                reserved_prompt_tokens = reserved_prompt_tokens - v_event.reserved_prompt_tokens,
                reserved_completion_tokens = reserved_completion_tokens - v_event.reserved_completion_tokens,
                reserved_cost_usd = reserved_cost_usd - v_event.reserved_cost_usd
            WHERE id = v_event.authorization_id AND owner_id = p_owner_id;
            RETURN v_event.id;
        END;
        $$;
        REVOKE ALL ON FUNCTION release_provider_spend_usage(uuid, varchar, jsonb) FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS provider_spend_usage_events CASCADE;
        DROP TABLE IF EXISTS provider_spend_authorizations CASCADE;
        DROP FUNCTION IF EXISTS release_provider_spend_usage(uuid, varchar, jsonb);
        DROP FUNCTION IF EXISTS settle_provider_spend_usage(uuid, varchar, integer, integer, numeric, jsonb);
        DROP FUNCTION IF EXISTS erase_own_provider_spend_children();
        DROP FUNCTION IF EXISTS provider_spend_usage_events_deny_mutation();
    """)
