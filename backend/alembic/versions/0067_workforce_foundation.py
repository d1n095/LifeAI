"""MainAI Internal Workforce Foundation (Stage T1–T7 schema + contracts).

Adds owner-scoped organizational tables ABOVE the existing Multi-Agent Work Coordination
layer (migration 0046 / `coordination_agents`). Does NOT duplicate provider spend,
execution envelopes, Vault/egress, or intelligence evidence — those remain the sole
enforcement/evidence systems; this foundation references them by id and records
organizational decisions.

Tables:
  - workforce_agent_profiles
  - workforce_teams
  - workforce_delegation_requests
  - workforce_context_packages
  - workforce_assignments
  - workforce_performance_rollups

See docs/LIFE_MAINAI_WORKFORCE_FOUNDATION.md for SAID vs IMPLEMENTED.
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


OWNER_SCOPED_TABLES = (
    "workforce_agent_profiles",
    "workforce_teams",
    "workforce_delegation_requests",
    "workforce_context_packages",
    "workforce_assignments",
    "workforce_performance_rollups",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE workforce_agent_profiles (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_key varchar(64) NOT NULL,
            name varchar(128) NOT NULL,
            role varchar(64) NOT NULL,
            agent_type varchar(64) NOT NULL,
            provider_type varchar(64) NOT NULL DEFAULT 'none',
            provider_model_id varchar(128),
            coordination_agent_id uuid REFERENCES coordination_agents(id) ON DELETE SET NULL,
            trust_zone varchar(32) NOT NULL DEFAULT 'UNTRUSTED_REMOTE',
            capability_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_tool_classes jsonb NOT NULL DEFAULT '[]'::jsonb,
            default_context_class varchar(64) NOT NULL DEFAULT 'task_local',
            risk_tier varchar(16) NOT NULL DEFAULT 'low',
            cost_class varchar(16) NOT NULL DEFAULT 'unknown',
            status varchar(32) NOT NULL DEFAULT 'candidate',
            version integer NOT NULL DEFAULT 1,
            configuration_fingerprint varchar(128) NOT NULL DEFAULT '',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            last_used_at timestamp,
            retired_at timestamp,
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_agent_profiles_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_workforce_agent_profiles_owner_key UNIQUE (owner_id, agent_key),
            CONSTRAINT ck_workforce_agent_profiles_status CHECK (status IN (
                'need_detected','candidate','sandbox','probation','active','disabled','retired'
            )),
            CONSTRAINT ck_workforce_agent_profiles_risk CHECK (risk_tier IN ('low','medium','high')),
            CONSTRAINT ck_workforce_agent_profiles_cost CHECK (cost_class IN ('low','medium','high','unknown')),
            CONSTRAINT ck_workforce_agent_profiles_caps_array CHECK (jsonb_typeof(capability_tags) = 'array'),
            CONSTRAINT ck_workforce_agent_profiles_tools_array CHECK (jsonb_typeof(allowed_tool_classes) = 'array'),
            CONSTRAINT ck_workforce_agent_profiles_prov_object CHECK (jsonb_typeof(provenance) = 'object'),
            CONSTRAINT ck_workforce_agent_profiles_version CHECK (version >= 1)
        );
        CREATE INDEX ix_workforce_agent_profiles_owner_status
            ON workforce_agent_profiles(owner_id, status);
        CREATE INDEX ix_workforce_agent_profiles_owner_trust
            ON workforce_agent_profiles(owner_id, trust_zone);
        CREATE INDEX ix_workforce_agent_profiles_coord
            ON workforce_agent_profiles(coordination_agent_id);

        CREATE TABLE workforce_teams (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name varchar(128) NOT NULL,
            pattern varchar(64) NOT NULL,
            member_profile_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(16) NOT NULL DEFAULT 'active',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            retired_at timestamp,
            CONSTRAINT uq_workforce_teams_id_owner UNIQUE (id, owner_id),
            CONSTRAINT ck_workforce_teams_status CHECK (status IN ('active','disabled','retired')),
            CONSTRAINT ck_workforce_teams_members_array CHECK (jsonb_typeof(member_profile_ids) = 'array'),
            CONSTRAINT ck_workforce_teams_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_teams_owner ON workforce_teams(owner_id);

        CREATE TABLE workforce_delegation_requests (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid,
            task_id uuid,
            goal_text text NOT NULL,
            required_capability varchar(128) NOT NULL,
            risk varchar(16) NOT NULL DEFAULT 'low',
            data_sensitivity varchar(32) NOT NULL DEFAULT 'internal',
            cost_ceiling_usd double precision,
            latency_preference varchar(32) NOT NULL DEFAULT 'balanced',
            verification_requirement varchar(64) NOT NULL DEFAULT 'independent_verifier',
            status varchar(32) NOT NULL DEFAULT 'open',
            selection_explanation jsonb NOT NULL DEFAULT '{}'::jsonb,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            resolved_at timestamp,
            CONSTRAINT uq_workforce_delegation_requests_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_workforce_delegation_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_workforce_delegation_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_workforce_delegation_risk CHECK (risk IN ('low','medium','high')),
            CONSTRAINT ck_workforce_delegation_status CHECK (status IN (
                'open','assigned','cancelled','failed','completed'
            )),
            CONSTRAINT ck_workforce_delegation_sel_object CHECK (jsonb_typeof(selection_explanation) = 'object'),
            CONSTRAINT ck_workforce_delegation_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_delegation_owner_status
            ON workforce_delegation_requests(owner_id, status);

        CREATE TABLE workforce_context_packages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            trust_zone varchar(32) NOT NULL,
            items jsonb NOT NULL DEFAULT '[]'::jsonb,
            denied_kinds jsonb NOT NULL DEFAULT '[]'::jsonb,
            disclosure_event_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            content_fingerprint varchar(64) NOT NULL DEFAULT '',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_context_packages_id_owner UNIQUE (id, owner_id),
            CONSTRAINT ck_workforce_context_items_array CHECK (jsonb_typeof(items) = 'array'),
            CONSTRAINT ck_workforce_context_denied_array CHECK (jsonb_typeof(denied_kinds) = 'array'),
            CONSTRAINT ck_workforce_context_disclosure_array CHECK (jsonb_typeof(disclosure_event_ids) = 'array'),
            CONSTRAINT ck_workforce_context_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_context_packages_owner ON workforce_context_packages(owner_id);

        CREATE TABLE workforce_assignments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            delegation_request_id uuid NOT NULL,
            profile_id uuid NOT NULL,
            team_id uuid,
            context_package_id uuid,
            coordination_assignment_id uuid,
            execution_envelope_id uuid,
            provider_spend_authorization_id uuid,
            allowed_read_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_write_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_tool_classes jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_network_destinations jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_project_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            spend_ceiling_usd double precision,
            allow_execution_effects boolean NOT NULL DEFAULT false,
            expires_at timestamp,
            revoked_at timestamp,
            revocation_reason text,
            verification_status varchar(16) NOT NULL DEFAULT 'UNVERIFIED',
            verifier_profile_id uuid,
            status varchar(32) NOT NULL DEFAULT 'assigned',
            result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            result_treated_as_data boolean NOT NULL DEFAULT true,
            selection_score jsonb NOT NULL DEFAULT '{}'::jsonb,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            completed_at timestamp,
            CONSTRAINT uq_workforce_assignments_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_workforce_assignments_request_owner FOREIGN KEY (delegation_request_id, owner_id)
                REFERENCES workforce_delegation_requests (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_workforce_assignments_profile_owner FOREIGN KEY (profile_id, owner_id)
                REFERENCES workforce_agent_profiles (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_workforce_assignments_team_owner FOREIGN KEY (team_id, owner_id)
                REFERENCES workforce_teams (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_workforce_assignments_ctx_owner FOREIGN KEY (context_package_id, owner_id)
                REFERENCES workforce_context_packages (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_workforce_assignments_verification CHECK (verification_status IN (
                'UNVERIFIED','CHECKED','VERIFIED','REJECTED','SUPERSEDED'
            )),
            CONSTRAINT ck_workforce_assignments_status CHECK (status IN (
                'assigned','running','awaiting_verification','completed','failed',
                'cancelled','revoked','expired','superseded'
            )),
            CONSTRAINT ck_workforce_assignments_read_array CHECK (jsonb_typeof(allowed_read_paths) = 'array'),
            CONSTRAINT ck_workforce_assignments_write_array CHECK (jsonb_typeof(allowed_write_paths) = 'array'),
            CONSTRAINT ck_workforce_assignments_tools_array CHECK (jsonb_typeof(allowed_tool_classes) = 'array'),
            CONSTRAINT ck_workforce_assignments_net_array CHECK (jsonb_typeof(allowed_network_destinations) = 'array'),
            CONSTRAINT ck_workforce_assignments_proj_array CHECK (jsonb_typeof(allowed_project_ids) = 'array'),
            CONSTRAINT ck_workforce_assignments_result_object CHECK (jsonb_typeof(result_payload) = 'object'),
            CONSTRAINT ck_workforce_assignments_score_object CHECK (jsonb_typeof(selection_score) = 'object'),
            CONSTRAINT ck_workforce_assignments_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_assignments_owner_status
            ON workforce_assignments(owner_id, status);
        CREATE INDEX ix_workforce_assignments_profile
            ON workforce_assignments(profile_id);
        CREATE INDEX ix_workforce_assignments_request
            ON workforce_assignments(delegation_request_id);

        CREATE TABLE workforce_performance_rollups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            profile_id uuid NOT NULL,
            capability_tag varchar(128) NOT NULL,
            jobs_attempted integer NOT NULL DEFAULT 0,
            jobs_completed integer NOT NULL DEFAULT 0,
            verified_success integer NOT NULL DEFAULT 0,
            verified_failure integer NOT NULL DEFAULT 0,
            founder_corrections integer NOT NULL DEFAULT 0,
            reviewer_corrections integer NOT NULL DEFAULT 0,
            latency_ms_sum integer NOT NULL DEFAULT 0,
            provider_cost_usd_sum double precision NOT NULL DEFAULT 0,
            token_usage_sum integer NOT NULL DEFAULT 0,
            tool_cost_usd_sum double precision NOT NULL DEFAULT 0,
            hallucination_or_factual_errors integer NOT NULL DEFAULT 0,
            security_violations integer NOT NULL DEFAULT 0,
            authority_violations integer NOT NULL DEFAULT 0,
            recovery_failures integer NOT NULL DEFAULT 0,
            quality_score_sum double precision NOT NULL DEFAULT 0,
            quality_score_count integer NOT NULL DEFAULT 0,
            domains_of_strength jsonb NOT NULL DEFAULT '[]'::jsonb,
            domains_of_weakness jsonb NOT NULL DEFAULT '[]'::jsonb,
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_performance_rollups_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_workforce_performance_profile_cap UNIQUE (owner_id, profile_id, capability_tag),
            CONSTRAINT fk_workforce_performance_profile_owner FOREIGN KEY (profile_id, owner_id)
                REFERENCES workforce_agent_profiles (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_workforce_performance_strength_array CHECK (jsonb_typeof(domains_of_strength) = 'array'),
            CONSTRAINT ck_workforce_performance_weakness_array CHECK (jsonb_typeof(domains_of_weakness) = 'array'),
            CONSTRAINT ck_workforce_performance_nonneg CHECK (
                jobs_attempted >= 0 AND jobs_completed >= 0 AND verified_success >= 0
                AND verified_failure >= 0 AND founder_corrections >= 0 AND reviewer_corrections >= 0
                AND latency_ms_sum >= 0 AND token_usage_sum >= 0
                AND hallucination_or_factual_errors >= 0 AND security_violations >= 0
                AND authority_violations >= 0 AND recovery_failures >= 0 AND quality_score_count >= 0
            )
        );
        CREATE INDEX ix_workforce_performance_owner_profile
            ON workforce_performance_rollups(owner_id, profile_id);
    """)

    for table in OWNER_SCOPED_TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """)


def downgrade() -> None:
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
