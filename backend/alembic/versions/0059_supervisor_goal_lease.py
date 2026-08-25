"""Supervisor Goal Lease -- the durable mutual-exclusion primitive that makes the production
Supervisor entry point (app/development_supervisor/production_entry.py) safe under real
worker concurrency and crashes. Founder decision (execution authority model, section 10):
"the autonomous trigger must be durable and idempotent" and must be attacked with two-workers-
same-goal, crash-before/during-Supervisor-start, and stale-lease/takeover scenarios.

`run_supervisor()` is a single bounded call (SupervisorBounds.max_elapsed_seconds, default
900s) meant to be invoked REPEATEDLY by an external driver across many worker ticks for the
same goal -- it is not a one-shot job the existing `mainai_jobs` claim/lease machinery
(app/jobs/mainai_job_lease.py) fits naturally (that table's rows are one-shot units of work
that reach a terminal status; a goal's Supervisor loop keeps being re-entered as new tasks
become ready). This table is therefore a NEW, narrow lease -- not a second job queue, not a
second control plane, not a second execution history: it records nothing about WHAT happened
during a run (that remains `mainai_checkpoints`/`SupervisorResult`, entirely unchanged), only
WHO currently holds the exclusive right to call `run_supervisor()` for a given goal.

Same fencing shape as `agent_scope_leases` (migration 0046, PR #132's own stale-lease-expiry
tick precedent) applied to a different resource: `lease_generation` bumped by exactly 1 on
every claim/reclaim, a partial unique index enforcing at most one ACTIVE lease per goal (a
takeover mutates the existing row rather than ever creating a second concurrently-active one),
`expires_at` the sole authority for "is this lease still alive" -- a crashed worker's lease is
never trusted past its TTL, and a worker that outlives its own lease (still running past
`expires_at`) MUST notice via the SAME fenced UPDATE...RETURNING re-verification every other
lease-write in this codebase already uses (never a bare `if` on a value read moments earlier).

`goal_id` is a composite owner-anchored FK to `mainai_goals(id, owner_id)`, reusing migration
0032's existing `uq_mainai_goals_id_owner_id` constraint exactly like migration 0057 did for
`execution_scope_proposals`/`execution_authorization_envelopes` -- applying the PR #140 lesson
proactively rather than waiting for another review to catch a bare FK."""

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE supervisor_goal_leases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            envelope_id uuid,
            worker_id varchar(128) NOT NULL,
            lease_generation integer NOT NULL DEFAULT 1,
            status varchar(16) NOT NULL DEFAULT 'active',
            acquired_at timestamp NOT NULL DEFAULT now(),
            expires_at timestamp NOT NULL,
            last_heartbeat_at timestamp,
            released_at timestamp,
            CONSTRAINT uq_supervisor_goal_leases_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_supervisor_goal_leases_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_supervisor_goal_leases_envelope_owner FOREIGN KEY (envelope_id, owner_id)
                REFERENCES execution_authorization_envelopes (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_supervisor_goal_leases_status CHECK (status IN ('active', 'released')),
            CONSTRAINT ck_supervisor_goal_leases_generation CHECK (lease_generation >= 1)
        );
        CREATE INDEX ix_supervisor_goal_leases_owner_goal ON supervisor_goal_leases(owner_id, goal_id);
        CREATE INDEX ix_supervisor_goal_leases_status_expires ON supervisor_goal_leases(status, expires_at);
        -- At most one ACTIVE lease per goal: a takeover/renewal must mutate the existing row
        -- (fencing the generation forward), never create a second concurrently-active one.
        CREATE UNIQUE INDEX uq_supervisor_goal_leases_one_active_per_goal
            ON supervisor_goal_leases(goal_id) WHERE status = 'active';

        ALTER TABLE supervisor_goal_leases ENABLE ROW LEVEL SECURITY;
        ALTER TABLE supervisor_goal_leases FORCE ROW LEVEL SECURITY;
        CREATE POLICY supervisor_goal_leases_isolation ON supervisor_goal_leases
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_supervisor_goal_leases() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_supervisor_goal_leases requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.supervisor_goal_leases WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_supervisor_goal_leases() FROM PUBLIC;
    """)

    # No explicit GRANT here, matching every migration since 0004 -- mainai_app's access comes
    # from the ALTER DEFAULT PRIVILEGES set up once by backend/db-init/01-app-role.sh /
    # backend/scripts/security/ensure_app_role.py. Deletion is intended to happen ONLY through
    # erase_own_supervisor_goal_leases() -- the Python service layer never issues a raw DELETE.


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_supervisor_goal_leases();")
    op.execute("DROP TABLE supervisor_goal_leases;")
