"""Multi-Agent Work Coordination foundation.

Adds the coordination layer that lets Life assign multiple external coding agents (Claude
Code, Cursor Agent, Codex, future CLI/API providers) to already-authorized MainAI goals/tasks
without two agents accidentally writing into overlapping repository scope at the same time.
Sits ABOVE the existing `mainai_goals`/`mainai_tasks` (migration 0032) — never a second
task/job queue. Capability/performance evidence is referenced through the existing
`intelligence_executions`/`intelligence_evidence` tables (migration 0038), never duplicated.

Five tables:
  - `coordination_agents` — provider-neutral agent registry. Deliberately NOT owner-scoped/
    RLS-protected, same reasoning `engineering_lessons` (migration 0032) already documents for
    itself: which coding agents/tools exist is founder-wide system knowledge, not per-owner
    personal data. Never stores a credential.
  - `parallel_exploration_groups` — groups independent assignments intentionally solving the
    SAME canonical problem in isolated branches/worktrees.
  - `agent_work_assignments` — one durable agent job: who, what (goal/task), where
    (repository/branch/worktree/allowed_paths), and with what authority (read/write mode,
    risk, approval_required as descriptive metadata only — the real approval gate remains
    `app.mainai_execution.approval.require_task_approval()`, unchanged).
  - `agent_work_assignment_dependencies` — dependency edges, mirrors
    `mainai_task_dependencies`'s exact shape.
  - `agent_scope_leases` — the deterministic write-conflict-prevention primitive.
    `lease_generation` follows `mainai_jobs.lease_generation`'s exact fencing convention
    (bumped by exactly 1 on takeover, in place — never a new row); staleness is always a
    timestamp comparison at read time, never a stored status value, matching
    `mainai_jobs.lease_expires_at`.
  - `agent_work_assignment_events` — append-only history, mirrors `mainai_task_events`'s exact
    shape and its own trigger-only append-only enforcement (RLS + BEFORE UPDATE/DELETE deny
    trigger) — the same precedent `intelligence_governance_deny_mutation` (migration 0038)
    already establishes for tables added after the earlier `mainai_job_events`-family, which
    additionally locks down `mainai_app`'s raw GRANT privileges (see `app/rls.py`). That
    heavier privilege-lockdown-and-boot-verification layer is reserved for that earlier,
    already-hardened subsystem; this migration follows the newer, equally-enforced (the
    trigger blocks UPDATE/DELETE unconditionally regardless of the calling role's raw grants)
    but simpler precedent.

See docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md for the full architecture and reuse rationale.
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


OWNER_SCOPED_TABLES = (
    "parallel_exploration_groups",
    "agent_work_assignments",
    "agent_work_assignment_dependencies",
    "agent_scope_leases",
    "agent_work_assignment_events",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE coordination_agents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_key varchar(64) NOT NULL UNIQUE,
            display_name varchar(128) NOT NULL,
            adapter_kind varchar(16) NOT NULL,
            capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            execution_mode varchar(32) NOT NULL DEFAULT 'unknown',
            supports_read boolean NOT NULL DEFAULT true,
            supports_write boolean NOT NULL DEFAULT false,
            model_hint varchar(128),
            cost_class varchar(16) NOT NULL DEFAULT 'unknown',
            concurrency_limit integer NOT NULL DEFAULT 1,
            status varchar(16) NOT NULL DEFAULT 'active',
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_coordination_agents_adapter_kind CHECK (adapter_kind IN ('cli','api','internal')),
            CONSTRAINT ck_coordination_agents_status CHECK (status IN ('active','unavailable','disabled')),
            CONSTRAINT ck_coordination_agents_cost_class CHECK (cost_class IN ('low','medium','high','unknown')),
            CONSTRAINT ck_coordination_agents_concurrency CHECK (concurrency_limit >= 1),
            CONSTRAINT ck_coordination_agents_capabilities_array CHECK (jsonb_typeof(capabilities) = 'array')
        );

        CREATE TABLE parallel_exploration_groups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            canonical_problem_ref varchar(256) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'open',
            synthesis_result_ref varchar(256),
            created_by varchar(64) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            resolved_at timestamp,
            CONSTRAINT uq_parallel_exploration_groups_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_parallel_exploration_groups_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_parallel_exploration_groups_status CHECK
                (status IN ('open','synthesizing','resolved','abandoned'))
        );
        CREATE INDEX ix_parallel_exploration_groups_owner ON parallel_exploration_groups(owner_id);
        CREATE INDEX ix_parallel_exploration_groups_goal ON parallel_exploration_groups(goal_id);

        CREATE TABLE agent_work_assignments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            task_id uuid,
            agent_id uuid NOT NULL REFERENCES coordination_agents(id),
            role varchar(16) NOT NULL,
            read_write_mode varchar(16) NOT NULL,
            repository_identity varchar(512) NOT NULL,
            allowed_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(24) NOT NULL DEFAULT 'planned',
            branch varchar(255),
            worktree_path text,
            base_sha varchar(64),
            head_sha varchar(64),
            upstream_pr_ref varchar(256),
            risk_level varchar(16) NOT NULL DEFAULT 'low',
            approval_required boolean NOT NULL DEFAULT false,
            parallel_exploration_group_id uuid,
            canonical_work_key varchar(256) NOT NULL,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            output_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            intelligence_execution_id uuid,
            requested_by varchar(64) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            started_at timestamp,
            completed_at timestamp,
            CONSTRAINT uq_agent_work_assignments_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_agent_work_assignments_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_agent_work_assignments_task_owner FOREIGN KEY (task_id, owner_id)
                REFERENCES mainai_tasks (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_agent_work_assignments_peg_owner FOREIGN KEY (parallel_exploration_group_id, owner_id)
                REFERENCES parallel_exploration_groups (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT fk_agent_work_assignments_execution_owner FOREIGN KEY (intelligence_execution_id, owner_id)
                REFERENCES intelligence_executions (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_agent_work_assignments_role CHECK
                (role IN ('builder','reviewer','tester','challenger','researcher','synthesizer')),
            CONSTRAINT ck_agent_work_assignments_rw_mode CHECK (read_write_mode IN ('read_only','read_write')),
            CONSTRAINT ck_agent_work_assignments_status CHECK (status IN (
                'planned','ready','running','waiting_dependency','waiting_agent','waiting_review',
                'blocked','ready_for_review','reviewing','changes_requested','verified','completed',
                'failed','cancelled','superseded'
            )),
            CONSTRAINT ck_agent_work_assignments_risk CHECK (risk_level IN ('low','medium','high')),
            CONSTRAINT ck_agent_work_assignments_paths_array CHECK (jsonb_typeof(allowed_paths) = 'array'),
            CONSTRAINT ck_agent_work_assignments_output_refs_array CHECK (jsonb_typeof(output_refs) = 'array'),
            CONSTRAINT ck_agent_work_assignments_provenance_object CHECK (jsonb_typeof(provenance) = 'object'),
            -- A REVIEWER/RESEARCHER holding read_write='read_write' would silently defeat the
            -- "a REVIEWER must be strictly READ_ONLY" requirement -- enforced structurally, not
            -- left to caller discipline.
            CONSTRAINT ck_agent_work_assignments_reviewer_read_only CHECK (
                role NOT IN ('reviewer', 'researcher') OR read_write_mode = 'read_only'
            )
        );
        CREATE INDEX ix_agent_work_assignments_owner_goal ON agent_work_assignments(owner_id, goal_id);
        CREATE INDEX ix_agent_work_assignments_owner_status ON agent_work_assignments(owner_id, status);
        CREATE INDEX ix_agent_work_assignments_agent ON agent_work_assignments(agent_id);
        CREATE INDEX ix_agent_work_assignments_owner_work_key ON agent_work_assignments(owner_id, canonical_work_key);
        CREATE INDEX ix_agent_work_assignments_peg ON agent_work_assignments(parallel_exploration_group_id);

        CREATE TABLE agent_work_assignment_dependencies (
            assignment_id uuid NOT NULL,
            depends_on_assignment_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamp NOT NULL DEFAULT now(),
            PRIMARY KEY (assignment_id, depends_on_assignment_id),
            CONSTRAINT fk_awad_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_awad_depends_on_owner FOREIGN KEY (depends_on_assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_awad_not_self CHECK (assignment_id <> depends_on_assignment_id)
        );
        CREATE INDEX ix_awad_owner ON agent_work_assignment_dependencies(owner_id);
        CREATE INDEX ix_awad_depends_on ON agent_work_assignment_dependencies(depends_on_assignment_id);

        CREATE TABLE agent_scope_leases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            agent_id uuid NOT NULL REFERENCES coordination_agents(id),
            repository_identity varchar(512) NOT NULL,
            branch varchar(255) NOT NULL,
            worktree_path text NOT NULL,
            allowed_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            mode varchar(16) NOT NULL,
            lease_generation integer NOT NULL DEFAULT 1,
            status varchar(16) NOT NULL DEFAULT 'active',
            acquired_at timestamp NOT NULL DEFAULT now(),
            expires_at timestamp,
            last_heartbeat_at timestamp,
            released_at timestamp,
            CONSTRAINT uq_agent_scope_leases_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_agent_scope_leases_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_agent_scope_leases_mode CHECK (mode IN ('read_only','read_write')),
            CONSTRAINT ck_agent_scope_leases_status CHECK (status IN ('active','released','superseded')),
            CONSTRAINT ck_agent_scope_leases_generation CHECK (lease_generation >= 1),
            CONSTRAINT ck_agent_scope_leases_paths_array CHECK (jsonb_typeof(allowed_paths) = 'array')
        );
        CREATE INDEX ix_agent_scope_leases_owner_repo_status
            ON agent_scope_leases(owner_id, repository_identity, status);
        CREATE INDEX ix_agent_scope_leases_assignment ON agent_scope_leases(assignment_id);
        -- At most one ACTIVE lease per assignment: a takeover/renewal must mutate the
        -- existing row (fencing the generation forward) rather than a caller accidentally
        -- creating a second concurrently-active lease for the same assignment.
        CREATE UNIQUE INDEX uq_agent_scope_leases_one_active_per_assignment
            ON agent_scope_leases(assignment_id) WHERE status = 'active';

        CREATE TABLE agent_work_assignment_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            assignment_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT fk_awae_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_awae_event_type CHECK (event_type IN (
                'created','agent_assigned','lease_acquired','lease_renewed','lease_taken_over',
                'lease_released','status_changed','dependency_satisfied','evidence_recorded',
                'conflict_detected'
            )),
            CONSTRAINT ck_awae_detail_object CHECK (jsonb_typeof(detail) = 'object')
        );
        CREATE INDEX ix_awae_assignment_created ON agent_work_assignment_events(assignment_id, created_at);
        CREATE INDEX ix_awae_owner ON agent_work_assignment_events(owner_id);
    """)

    for table in OWNER_SCOPED_TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """)

    op.execute("""
        CREATE FUNCTION agent_work_assignment_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.agent_coordination_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'agent_work_assignment_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'agent_work_assignment_events is append-only: UPDATE is never permitted.';
        END;
        $$;
        REVOKE ALL ON FUNCTION agent_work_assignment_events_deny_mutation() FROM PUBLIC;
        CREATE TRIGGER trg_agent_work_assignment_events_deny_mutation
            BEFORE UPDATE OR DELETE ON agent_work_assignment_events
            FOR EACH ROW EXECUTE FUNCTION agent_work_assignment_events_deny_mutation();
    """)

    op.execute("""
        CREATE FUNCTION erase_own_agent_coordination_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_agent_coordination_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.agent_coordination_erasure_in_progress', 'on', true);
            DELETE FROM public.agent_work_assignment_events WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_scope_leases WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_work_assignment_dependencies WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_work_assignments WHERE owner_id = v_owner_id;
            DELETE FROM public.parallel_exploration_groups WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_agent_coordination_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_agent_coordination_children();")
    op.execute("DROP TRIGGER trg_agent_work_assignment_events_deny_mutation ON agent_work_assignment_events;")
    op.execute("DROP FUNCTION agent_work_assignment_events_deny_mutation();")
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE {table};")
    op.execute("DROP TABLE coordination_agents;")
