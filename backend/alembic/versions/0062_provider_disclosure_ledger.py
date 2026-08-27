"""Life Vault / External-AI Egress Control -- provider disclosure ledger.

WHY THIS EXISTS (see docs/LIFE_VAULT_EGRESS_CONTROL.md for the full threat model): every
external AI provider (OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, future local/Qwen
providers) is treated as a potentially untrusted data recipient. Before this migration there
was no durable record of what content was ever actually sent to a provider -- existing usage
tables (`provider_spend_usage_events`) record token counts and cost only, never content.
The founder must eventually be able to ask "what has provider X ever received about project
Y?" and get a complete, honest answer. This table is that answer.

Structural invariants this table exists to make durable, not just documented:
  - DEFAULT EGRESS = DENY: every call to the egress gate (app/egress_policy/service.py)
    produces exactly one row here, whether the decision was 'allowed' or 'denied' -- a
    call that was refused is just as much an audit fact as one that succeeded.
  - Content is never stored raw. Only `attempted_content_hash` (what was originally
    requested) and `sent_content_hash` (what, if anything, actually left the process after
    redaction -- NULL when denied) are recorded, matching migration 0033/0060's own "hashes,
    never raw SECRET" discipline elsewhere in this codebase.
  - Append-only at the database level, same pattern as `mainai_recovery_events`/
    `provider_spend_usage_events` (migration 0033/0060): UPDATE is unconditionally denied for
    every role, no GUC escape hatch -- a disclosure ledger that could be silently edited after
    the fact defeats its entire purpose. DELETE only through an authorized owner erasure.
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE provider_disclosure_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider varchar(64) NOT NULL,
            model varchar(128) NOT NULL,
            purpose varchar(64) NOT NULL,
            requested_by varchar(128) NOT NULL,
            task_id uuid,
            goal_id uuid,
            job_id uuid,
            decision varchar(16) NOT NULL,
            reason text NOT NULL,
            redaction_categories jsonb NOT NULL DEFAULT '[]'::jsonb,
            attempted_content_hash varchar(64) NOT NULL,
            sent_content_hash varchar(64),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_provider_disclosure_events_decision CHECK (decision IN ('allowed', 'denied')),
            CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                (decision = 'denied' AND sent_content_hash IS NULL)
                OR (decision = 'allowed' AND sent_content_hash IS NOT NULL)
            ),
            CONSTRAINT ck_provider_disclosure_events_redaction_categories CHECK (
                jsonb_typeof(redaction_categories) = 'array'
            )
        );
        CREATE INDEX ix_provider_disclosure_events_owner_provider
            ON provider_disclosure_events(owner_id, provider, created_at);
        CREATE INDEX ix_provider_disclosure_events_owner_created
            ON provider_disclosure_events(owner_id, created_at);

        ALTER TABLE provider_disclosure_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE provider_disclosure_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY provider_disclosure_events_isolation ON provider_disclosure_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        CREATE FUNCTION provider_disclosure_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.provider_disclosure_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'provider_disclosure_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'provider_disclosure_events is append-only: UPDATE is never permitted.';
        END;
        $$;
        REVOKE ALL ON FUNCTION provider_disclosure_events_deny_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_provider_disclosure_events_deny_mutation
            BEFORE UPDATE OR DELETE ON provider_disclosure_events
            FOR EACH ROW EXECUTE FUNCTION provider_disclosure_events_deny_mutation();

        CREATE FUNCTION erase_own_provider_disclosure_events() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_provider_disclosure_events requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.provider_disclosure_erasure_in_progress', 'on', true);
            DELETE FROM public.provider_disclosure_events WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_provider_disclosure_events() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS provider_disclosure_events CASCADE;
        DROP FUNCTION IF EXISTS erase_own_provider_disclosure_events();
        DROP FUNCTION IF EXISTS provider_disclosure_events_deny_mutation();
    """)
