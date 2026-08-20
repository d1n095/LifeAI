"""Interactive Agent Execution Control Foundation.

Extends the dispatch foundation (migration 0046, PR #83-#87) from a bounded "start-then-
collect" model into a provider-neutral execution-CONTROL model that can track a real,
long-running agent process/session -- without inventing a second supervisor, a second
task/job queue, or a second evidence store.

One new table, one CHECK-constraint extension, no new native Postgres enum type:

- `agent_dispatch_executions` -- the live, mutable "current state" of ONE dispatch attempt
  (correlates with `dispatch.DispatchDecision.attempt_id`). Distinct from
  `agent_work_assignment_events` (append-only HISTORY) the same way `agent_scope_leases` is
  distinct from `agent_work_assignments` -- a live row that gets updated in place
  (`last_heartbeat_at`, `last_output_at`, `adapter_state`) as a real process runs, not a log.
  One row per attempt (an assignment retried after a failure gets a second row, correlated by
  its own fresh `attempt_id` -- never overwrites the previous attempt's own history).
- `ck_awae_event_type` (on the EXISTING `agent_work_assignment_events` table) gains exactly
  one new value, `execution_observed` -- the durable audit trail for structured execution
  events (status/progress/tool_action/heartbeat/partial_result/final_result). This is a plain
  `varchar` + CHECK constraint, not a native Postgres ENUM type (see migration 0046's own DDL),
  so extending it needs no `ALTER TYPE` -- just a constraint swap.

See docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's "Interactive Agent Execution Control"
section for the full architecture and why raw stdout/stderr volume is deliberately NOT
persisted here by default (only its arrival TIME, via `last_output_at`) while every other
event kind is.
"""

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


_OLD_EVENT_TYPES = (
    "created", "agent_assigned", "lease_acquired", "lease_renewed", "lease_taken_over",
    "lease_released", "status_changed", "dependency_satisfied", "evidence_recorded",
    "conflict_detected",
)
_NEW_EVENT_TYPES = _OLD_EVENT_TYPES + ("execution_observed",)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_dispatch_executions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            adapter_key varchar(64) NOT NULL,
            process_ref varchar(256),
            adapter_state varchar(24) NOT NULL DEFAULT 'starting',
            result_ingestion_status varchar(16) NOT NULL DEFAULT 'pending',
            started_at timestamp NOT NULL DEFAULT now(),
            last_heartbeat_at timestamp,
            last_output_at timestamp,
            ended_at timestamp,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_dispatch_executions_attempt UNIQUE (attempt_id),
            CONSTRAINT fk_agent_dispatch_executions_assignment_owner FOREIGN KEY (assignment_id, owner_id)
                REFERENCES agent_work_assignments (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_agent_dispatch_executions_adapter_state CHECK (adapter_state IN (
                'starting', 'running', 'exited', 'lost', 'timeout', 'cancelled'
            )),
            CONSTRAINT ck_agent_dispatch_executions_ingestion_status CHECK (result_ingestion_status IN (
                'pending', 'ingested', 'failed'
            ))
        );
        CREATE INDEX ix_agent_dispatch_executions_owner_assignment
            ON agent_dispatch_executions(owner_id, assignment_id);

        ALTER TABLE agent_work_assignment_events DROP CONSTRAINT ck_awae_event_type;
        ALTER TABLE agent_work_assignment_events ADD CONSTRAINT ck_awae_event_type CHECK (event_type IN (
            'created', 'agent_assigned', 'lease_acquired', 'lease_renewed', 'lease_taken_over',
            'lease_released', 'status_changed', 'dependency_satisfied', 'evidence_recorded',
            'conflict_detected', 'execution_observed'
        ));
    """)

    op.execute("""
        ALTER TABLE agent_dispatch_executions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE agent_dispatch_executions FORCE ROW LEVEL SECURITY;
        CREATE POLICY agent_dispatch_executions_isolation ON agent_dispatch_executions
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # CREATE OR REPLACE, not a new function -- same erasure entry point migration 0046 already
    # wired into erase_account_data(), now additionally covering the new table. Function
    # ownership/EXECUTE grant to mainai_app is unaffected by a body replacement.
    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_agent_coordination_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_agent_coordination_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.agent_coordination_erasure_in_progress', 'on', true);
            DELETE FROM public.agent_dispatch_executions WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_work_assignment_events WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_scope_leases WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_work_assignment_dependencies WHERE owner_id = v_owner_id;
            DELETE FROM public.agent_work_assignments WHERE owner_id = v_owner_id;
            DELETE FROM public.parallel_exploration_groups WHERE owner_id = v_owner_id;
        END;
        $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_agent_coordination_children() RETURNS void
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

        ALTER TABLE agent_work_assignment_events DROP CONSTRAINT ck_awae_event_type;
        ALTER TABLE agent_work_assignment_events ADD CONSTRAINT ck_awae_event_type CHECK (event_type IN (
            'created', 'agent_assigned', 'lease_acquired', 'lease_renewed', 'lease_taken_over',
            'lease_released', 'status_changed', 'dependency_satisfied', 'evidence_recorded',
            'conflict_detected'
        ));

        DROP TABLE agent_dispatch_executions;
    """)
