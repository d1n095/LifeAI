"""Workforce ops tables (T8–T16/T19/T20) — migration 0068.

Extends 0067 foundation with durable checkpoints (failure/takeover), lifecycle events
(hiring/learning), and cost budget envelopes. Does NOT duplicate provider_spend —
budget rows are organizational ceilings that gate broker selection; real spend still
goes through app.provider_spend reserve/settle.
"""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


OWNER_SCOPED_TABLES = (
    "workforce_assignment_checkpoints",
    "workforce_lifecycle_events",
    "workforce_cost_budgets",
    "workforce_verification_decisions",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE workforce_assignment_checkpoints (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            checkpoint_kind varchar(32) NOT NULL,
            sequence integer NOT NULL DEFAULT 1,
            partial_result jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            external_effect_state varchar(32) NOT NULL DEFAULT 'none_known',
            -- none_known | none_proven | unknown | effect_proven
            failure_class varchar(64),
            recoverable boolean NOT NULL DEFAULT true,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_checkpoints_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_workforce_checkpoints_assignment_seq UNIQUE (owner_id, assignment_id, sequence),
            CONSTRAINT fk_workforce_checkpoints_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES workforce_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_workforce_checkpoints_kind CHECK (checkpoint_kind IN (
                'progress','partial_result','crash','timeout','rate_limit','malformed',
                'lost_lease','restart','takeover','resume'
            )),
            CONSTRAINT ck_workforce_checkpoints_effect CHECK (external_effect_state IN (
                'none_known','none_proven','unknown','effect_proven'
            )),
            CONSTRAINT ck_workforce_checkpoints_seq CHECK (sequence >= 1),
            CONSTRAINT ck_workforce_checkpoints_partial_object CHECK (jsonb_typeof(partial_result) = 'object'),
            CONSTRAINT ck_workforce_checkpoints_evidence_array CHECK (jsonb_typeof(evidence_refs) = 'array'),
            CONSTRAINT ck_workforce_checkpoints_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_checkpoints_assignment
            ON workforce_assignment_checkpoints(assignment_id, sequence);

        CREATE TABLE workforce_lifecycle_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            profile_id uuid NOT NULL,
            from_status varchar(32),
            to_status varchar(32) NOT NULL,
            change_kind varchar(64) NOT NULL,
            change_summary text NOT NULL,
            evidence_before jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_after jsonb NOT NULL DEFAULT '{}'::jsonb,
            rollback_ref varchar(256),
            trained boolean NOT NULL DEFAULT false,
            -- trained=true ONLY when actual model training/fine-tune occurred
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_lifecycle_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_workforce_lifecycle_profile_owner FOREIGN KEY (profile_id, owner_id)
                REFERENCES workforce_agent_profiles (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_workforce_lifecycle_change CHECK (change_kind IN (
                'need_detected','candidate_created','enter_sandbox','benchmark',
                'adversarial_test','enter_probation','activate','improve_policy',
                'improve_tools','improve_playbook','improve_retrieval','provider_swap',
                'fine_tune','retire','disable','rollback'
            )),
            CONSTRAINT ck_workforce_lifecycle_before_object CHECK (jsonb_typeof(evidence_before) = 'object'),
            CONSTRAINT ck_workforce_lifecycle_after_object CHECK (jsonb_typeof(evidence_after) = 'object'),
            CONSTRAINT ck_workforce_lifecycle_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_lifecycle_profile
            ON workforce_lifecycle_events(owner_id, profile_id, created_at);

        CREATE TABLE workforce_cost_budgets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scope_kind varchar(32) NOT NULL,
            -- assignment | agent | team | goal | period | provider
            scope_ref varchar(128) NOT NULL,
            cap_usd double precision NOT NULL,
            spent_usd double precision NOT NULL DEFAULT 0,
            reserved_usd double precision NOT NULL DEFAULT 0,
            period_start timestamp,
            period_end timestamp,
            status varchar(16) NOT NULL DEFAULT 'active',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_cost_budgets_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_workforce_cost_budgets_scope UNIQUE (owner_id, scope_kind, scope_ref),
            CONSTRAINT ck_workforce_cost_scope CHECK (scope_kind IN (
                'assignment','agent','team','goal','period','provider'
            )),
            CONSTRAINT ck_workforce_cost_status CHECK (status IN ('active','exhausted','revoked','expired')),
            CONSTRAINT ck_workforce_cost_nonneg CHECK (
                cap_usd >= 0 AND spent_usd >= 0 AND reserved_usd >= 0
            ),
            CONSTRAINT ck_workforce_cost_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_cost_budgets_owner_status
            ON workforce_cost_budgets(owner_id, status);

        CREATE TABLE workforce_verification_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            decision varchar(16) NOT NULL,
            policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            verifier_profile_id uuid,
            second_verifier_profile_id uuid,
            test_evidence_ref varchar(256),
            deterministic_validator varchar(128),
            founder_approval_ref varchar(256),
            agreement boolean,
            reason text NOT NULL DEFAULT '',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_workforce_verification_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_workforce_verification_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES workforce_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_workforce_verification_decision CHECK (decision IN (
                'UNVERIFIED','CHECKED','VERIFIED','REJECTED','SUPERSEDED'
            )),
            CONSTRAINT ck_workforce_verification_policy_object CHECK (jsonb_typeof(policy_snapshot) = 'object'),
            CONSTRAINT ck_workforce_verification_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_workforce_verification_assignment
            ON workforce_verification_decisions(assignment_id, created_at);

        -- Failure/takeover bookkeeping on assignments (no authority widening).
        ALTER TABLE workforce_assignments
            ADD COLUMN supersedes_assignment_id uuid,
            ADD COLUMN takeover_of_assignment_id uuid,
            ADD COLUMN failure_class varchar(64),
            ADD COLUMN external_effect_state varchar(32) NOT NULL DEFAULT 'none_known',
            ADD COLUMN retry_count integer NOT NULL DEFAULT 0,
            ADD CONSTRAINT ck_workforce_assignments_effect CHECK (external_effect_state IN (
                'none_known','none_proven','unknown','effect_proven'
            )),
            ADD CONSTRAINT ck_workforce_assignments_retry CHECK (retry_count >= 0);
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
    op.execute("""
        ALTER TABLE workforce_assignments
            DROP CONSTRAINT IF EXISTS ck_workforce_assignments_retry,
            DROP CONSTRAINT IF EXISTS ck_workforce_assignments_effect,
            DROP COLUMN IF EXISTS retry_count,
            DROP COLUMN IF EXISTS external_effect_state,
            DROP COLUMN IF EXISTS failure_class,
            DROP COLUMN IF EXISTS takeover_of_assignment_id,
            DROP COLUMN IF EXISTS supersedes_assignment_id;
    """)
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
