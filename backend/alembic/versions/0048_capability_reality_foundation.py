"""Life Capability Reality / Self-Model foundation.

Answers the deferred layer named, verbatim or near-verbatim, in FIVE separate existing
foundations' own "Explicitly deferred" sections: "capability matrix and derived scoring"
(migration 0038), "automatic capability scoring" (0042), and repeated "world-model
planning"/"world-model reasoning" mentions (0038, 0041, 0042, 0043) that a capability
self-model is a prerequisite for. This migration builds ONLY the capability half of that gap
-- a queryable, evidence-backed record of what Life and the surrounding system can actually
do RIGHT NOW, distinct from what is merely configured, planned, or unknown. It introduces no
queue, executor, router, or second agent/adapter registry.

Two tables, mirroring the EXACT split already proven twice in this codebase (migration 0046's
`agent_scope_leases` vs `agent_work_assignment_events`, and migration 0047's
`agent_dispatch_executions` vs the same events table): one live, mutable "current state" row
per capability (`capability_records`), and one append-only observation history
(`capability_observation_events`) -- never the same concept duplicated as two competing
stores.

Explicitly reused, never duplicated:
  - `intelligence_evidence` (migration 0038) may be referenced as the concrete evidence a
    capability observation is grounded in -- `capability_records.last_verification_evidence_id`
    is a nullable FK, never a copy of evidence content.
  - `coordination_agents` (migration 0046) may be referenced when a capability IS an
    agent/adapter capability -- `capability_records.agent_id` is a nullable FK, never a
    second agent registry. `app.agent_coordination.adapter_config.adapter_availability()`
    remains the sole source of real adapter-availability facts; this migration's own service
    layer only TRANSLATES that into a capability_records row, never reimplements it.
  - The SAME `authority` closed vocabulary migration 0042 already established
    (`founder | repeated_founder_preference | deterministic_source | inferred_pattern |
    ai_interpretation | unknown`) -- reused verbatim, not reinvented, so "who/what asserted
    this capability fact" stays consistent across the whole Life Core family.

Hard rule, structural, not just documented: `status` is a closed, five-value vocabulary
(`verified_available | configured_unavailable | configured_disabled | planned | unknown`)
that a caller must supply explicitly every time -- nothing in this migration or the service
layer it supports infers `verified_available` from an executable existing, an adapter being
enabled, or any other indirect signal. Verification requires an explicit, caller-supplied
success observation.
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


TABLES = ("capability_records", "capability_observation_events")


def upgrade() -> None:
    op.execute("""
        CREATE TABLE capability_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            capability_key varchar(128) NOT NULL,
            domain varchar(64) NOT NULL,
            status varchar(24) NOT NULL DEFAULT 'unknown',
            status_reason text,
            authority varchar(40) NOT NULL DEFAULT 'unknown',
            required_permissions jsonb NOT NULL DEFAULT '[]'::jsonb,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            known_limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
            confidence numeric(5,4),
            agent_id uuid REFERENCES coordination_agents(id),
            last_verification_evidence_id uuid,
            last_verified_at timestamp,
            last_success_at timestamp,
            last_failure_at timestamp,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_capability_records_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_capability_records_owner_key UNIQUE (owner_id, capability_key),
            CONSTRAINT fk_capability_records_evidence_owner FOREIGN KEY (last_verification_evidence_id, owner_id)
                REFERENCES intelligence_evidence (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_capability_records_status CHECK (status IN (
                'verified_available', 'configured_unavailable', 'configured_disabled', 'planned', 'unknown'
            )),
            CONSTRAINT ck_capability_records_authority CHECK (authority IN (
                'founder', 'repeated_founder_preference', 'deterministic_source', 'inferred_pattern',
                'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_capability_records_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_capability_records_permissions_array CHECK (jsonb_typeof(required_permissions) = 'array'),
            CONSTRAINT ck_capability_records_dependencies_array CHECK (jsonb_typeof(dependencies) = 'array'),
            CONSTRAINT ck_capability_records_limitations_array CHECK (jsonb_typeof(known_limitations) = 'array'),
            CONSTRAINT ck_capability_records_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_capability_records_owner_domain ON capability_records(owner_id, domain);
        CREATE INDEX ix_capability_records_owner_status ON capability_records(owner_id, status);
        CREATE INDEX ix_capability_records_agent ON capability_records(agent_id);

        CREATE TABLE capability_observation_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            capability_record_id uuid NOT NULL,
            event_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT fk_capability_observation_events_record_owner FOREIGN KEY (capability_record_id, owner_id)
                REFERENCES capability_records (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_capability_observation_events_type CHECK (event_type IN (
                'status_changed', 'verification_recorded', 'success_recorded', 'failure_recorded',
                'gap_recorded', 'observation_reasserted'
            )),
            CONSTRAINT ck_capability_observation_events_detail CHECK (jsonb_typeof(detail) = 'object')
        );
        CREATE INDEX ix_capability_observation_events_record_created
            ON capability_observation_events(capability_record_id, created_at);
        CREATE INDEX ix_capability_observation_events_owner ON capability_observation_events(owner_id);
    """)

    for table in TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """)

    op.execute("""
        CREATE FUNCTION capability_observation_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('app.capability_reality_erasure_in_progress', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$;
        REVOKE ALL ON FUNCTION capability_observation_events_deny_mutation() FROM PUBLIC;
        CREATE TRIGGER trg_capability_observation_events_deny_mutation
            BEFORE UPDATE OR DELETE ON capability_observation_events
            FOR EACH ROW EXECUTE FUNCTION capability_observation_events_deny_mutation();
    """)

    op.execute("""
        CREATE FUNCTION erase_own_capability_reality_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_capability_reality_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.capability_reality_erasure_in_progress', 'on', true);
            DELETE FROM public.capability_observation_events WHERE owner_id = v_owner_id;
            DELETE FROM public.capability_records WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_capability_reality_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_capability_reality_children();")
    op.execute("DROP TRIGGER trg_capability_observation_events_deny_mutation ON capability_observation_events;")
    op.execute("DROP FUNCTION capability_observation_events_deny_mutation();")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table};")
