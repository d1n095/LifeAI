"""MainAI orchestration truth ledger.

Canonical occupancy + GitHub-backed software truth for coordinating external coding
agents. Agent text is stored as claims and is never authoritative for remote/CI/PR
state. Ledger rows do not grant merge, deploy, Recall, or RLS authority.

Revision ID: 0089_orchestration_truth_ledger
Revises: 0088_erasure_completion_phase
Create Date: 2026-09-29
"""

from alembic import op

revision = "0089_orchestration_truth_ledger"
down_revision = "0088_erasure_completion_phase"
branch_labels = None
depends_on = None

OWNER_SCOPED_TABLES = (
    "orchestration_agents",
    "orchestration_slots",
    "orchestration_tasks",
    "orchestration_task_dependencies",
    "orchestration_claims",
    "orchestration_github_snapshots",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE orchestration_agents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_key varchar(64) NOT NULL,
            display_name varchar(128) NOT NULL,
            occupancy_status varchar(32) NOT NULL DEFAULT 'idle',
            supports_parallel_workers boolean NOT NULL DEFAULT false,
            max_slots integer NOT NULL DEFAULT 1,
            current_task_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_orchestration_agents_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_orchestration_agents_owner_key UNIQUE (owner_id, agent_key),
            CONSTRAINT ck_orchestration_agents_status CHECK (occupancy_status IN ('idle','running','blocked')),
            CONSTRAINT ck_orchestration_agents_slots CHECK (max_slots >= 1),
            CONSTRAINT ck_orchestration_agents_prov CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_orchestration_agents_owner_status
            ON orchestration_agents (owner_id, occupancy_status);

        CREATE TABLE orchestration_slots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_id uuid NOT NULL,
            slot_key varchar(64) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'free',
            task_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_orchestration_slots_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_orchestration_slots_owner_agent_key UNIQUE (owner_id, agent_id, slot_key),
            CONSTRAINT fk_orchestration_slots_agent_owner FOREIGN KEY (agent_id, owner_id)
                REFERENCES orchestration_agents (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_orchestration_slots_status CHECK (status IN ('free','occupied'))
        );
        CREATE INDEX ix_orchestration_slots_owner_agent
            ON orchestration_slots (owner_id, agent_id, status);

        CREATE TABLE orchestration_tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_id uuid,
            assigned_slot_id uuid,
            title varchar(256) NOT NULL,
            role varchar(32) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'queued',
            exact_input_sha varchar(40),
            working_branch varchar(255),
            output_sha varchar(40),
            remote_sha varchar(40),
            remote_pushed boolean NOT NULL DEFAULT false,
            frozen boolean NOT NULL DEFAULT false,
            protects_sha varchar(40),
            protects_branch varchar(255),
            artifacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            test_runs jsonb NOT NULL DEFAULT '[]'::jsonb,
            blockers jsonb NOT NULL DEFAULT '[]'::jsonb,
            next_action jsonb NOT NULL DEFAULT '{}'::jsonb,
            authority_required varchar(64) NOT NULL DEFAULT 'none',
            last_verified_source varchar(64),
            last_verified_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_orchestration_tasks_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_orchestration_tasks_agent_owner FOREIGN KEY (agent_id, owner_id)
                REFERENCES orchestration_agents (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_orchestration_tasks_slot_owner FOREIGN KEY (assigned_slot_id, owner_id)
                REFERENCES orchestration_slots (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_orchestration_tasks_role CHECK (role IN (
                'builder','examiner','validator','researcher','operator'
            )),
            CONSTRAINT ck_orchestration_tasks_status CHECK (status IN (
                'queued','assigned','running','blocked','awaiting_external','completed','failed','cancelled'
            )),
            CONSTRAINT ck_orchestration_tasks_input_sha CHECK (
                exact_input_sha IS NULL OR exact_input_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_tasks_output_sha CHECK (
                output_sha IS NULL OR output_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_tasks_remote_sha CHECK (
                remote_sha IS NULL OR remote_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_tasks_protects_sha CHECK (
                protects_sha IS NULL OR protects_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_tasks_remote_pushed_github CHECK (
                remote_pushed = false
                OR (remote_sha IS NOT NULL AND last_verified_source = 'github')
            ),
            CONSTRAINT ck_orchestration_tasks_authority CHECK (authority_required IN (
                'none','founder_only','material_product_decision','security_policy',
                'money_budget','destructive_action','unresolved_alternatives','genuine_blocker'
            )),
            CONSTRAINT ck_orchestration_tasks_verified_source CHECK (
                last_verified_source IS NULL OR last_verified_source IN (
                    'github','pytest_execution','verification_registry','local_git','agent_claim'
                )
            ),
            CONSTRAINT ck_orchestration_tasks_artifacts CHECK (jsonb_typeof(artifacts) = 'array'),
            CONSTRAINT ck_orchestration_tasks_tests CHECK (jsonb_typeof(test_runs) = 'array'),
            CONSTRAINT ck_orchestration_tasks_blockers CHECK (jsonb_typeof(blockers) = 'array'),
            CONSTRAINT ck_orchestration_tasks_next_action CHECK (jsonb_typeof(next_action) = 'object')
        );
        CREATE INDEX ix_orchestration_tasks_owner_status ON orchestration_tasks (owner_id, status);
        CREATE INDEX ix_orchestration_tasks_owner_agent ON orchestration_tasks (owner_id, agent_id);
        CREATE INDEX ix_orchestration_tasks_branch ON orchestration_tasks (owner_id, working_branch);

        CREATE TABLE orchestration_task_dependencies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            depends_on_task_id uuid NOT NULL,
            dependency_kind varchar(32) NOT NULL DEFAULT 'blocks',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_orchestration_task_dependencies UNIQUE (task_id, depends_on_task_id, dependency_kind),
            CONSTRAINT fk_orchestration_dep_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES orchestration_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_orchestration_dep_parent_owner FOREIGN KEY (depends_on_task_id, owner_id)
                REFERENCES orchestration_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_orchestration_dep_kind CHECK (dependency_kind IN (
                'blocks','examines','validates','frozen_base','follows'
            )),
            CONSTRAINT ck_orchestration_dep_not_self CHECK (task_id <> depends_on_task_id)
        );

        CREATE TABLE orchestration_claims (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id uuid,
            agent_key varchar(64) NOT NULL,
            claim_kind varchar(64) NOT NULL,
            claimed_value jsonb NOT NULL DEFAULT '{}'::jsonb,
            raw_text text NOT NULL,
            bound_sha varchar(40),
            authoritative boolean NOT NULL DEFAULT false,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_orchestration_claims_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES orchestration_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_orchestration_claims_kind CHECK (claim_kind IN (
                'remote_pushed','tests_passed','examiner_result','branch_exists',
                'task_completed','stopped','blocked'
            )),
            CONSTRAINT ck_orchestration_claims_not_authoritative CHECK (authoritative = false),
            CONSTRAINT ck_orchestration_claims_sha CHECK (
                bound_sha IS NULL OR bound_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_claims_value CHECK (jsonb_typeof(claimed_value) = 'object')
        );
        CREATE INDEX ix_orchestration_claims_owner_task ON orchestration_claims (owner_id, task_id);

        CREATE TABLE orchestration_github_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id uuid,
            branch varchar(255) NOT NULL,
            exists_remotely boolean NOT NULL,
            commit_sha varchar(40),
            tree_sha varchar(40),
            local_sha varchar(40),
            local_matches_remote boolean,
            ci_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            pr_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            default_branch varchar(255),
            default_branch_sha varchar(40),
            deployments_payload jsonb NOT NULL DEFAULT '[]'::jsonb,
            captured_at timestamptz NOT NULL DEFAULT now(),
            source varchar(32) NOT NULL DEFAULT 'github',
            CONSTRAINT fk_orchestration_github_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES orchestration_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_orchestration_github_source CHECK (source = 'github'),
            CONSTRAINT ck_orchestration_github_commit CHECK (
                commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_github_tree CHECK (
                tree_sha IS NULL OR tree_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_github_local CHECK (
                local_sha IS NULL OR local_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_github_default_sha CHECK (
                default_branch_sha IS NULL OR default_branch_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_orchestration_github_ci CHECK (jsonb_typeof(ci_payload) = 'object'),
            CONSTRAINT ck_orchestration_github_pr CHECK (jsonb_typeof(pr_payload) = 'object'),
            CONSTRAINT ck_orchestration_github_deploys CHECK (jsonb_typeof(deployments_payload) = 'array')
        );
        CREATE INDEX ix_orchestration_github_owner_branch
            ON orchestration_github_snapshots (owner_id, branch, captured_at DESC);
        """
    )

    for table in OWNER_SCOPED_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
            """
        )


def downgrade() -> None:
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
