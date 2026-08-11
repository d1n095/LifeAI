"""MainAI Execution Loop V0.1 — durable Goal/Plan/Task/Checkpoint schema, plus the engineering
lesson (safety memory) foundation.

This is a NEW table family sitting ABOVE the existing `mainai_jobs` durable runtime
(migrations 0026-0029), not a replacement or a parallel job/lease/heartbeat system. A `Goal`
is a founder-given objective; a `Plan` is a versioned, structured breakdown of that goal into
`MainAITask` rows with dependencies; when a task becomes ready, its actual execution unit is a
real `mainai_jobs` row (a new `job_type`, dispatched through the existing
`claim_next_mainai_job`/`renew_mainai_job_lease`/`_guarded_job_write` machinery in
`app/jobs/mainai_job_lease.py`/`app/rag/mainai_jobs_service.py`) — `mainai_tasks.mainai_job_id`
is the pointer from a task to its current-or-last execution job. This migration adds ZERO new
lease/fencing/heartbeat columns anywhere: liveness/retry/cancellation for actual work
continues to be governed entirely by the already-reviewed `mainai_jobs` primitives (see that
table's own migrations for the BLOCKER-round lease-fencing incident this deliberately avoids
re-litigating).

Six tables:

  - `mainai_goals`: one durable, owner-scoped run. `status` is intentionally richer than
    `mainai_jobs.status` (pending/planning/running/waiting/blocked/failed/completed/cancelled)
    because a goal's lifecycle spans planning AND execution, not just one queued unit of work.
  - `mainai_plans`: versioned, append-only breakdown of a goal — a replan creates a NEW row
    (`version` = previous max + 1), never edits or deletes an earlier version's history. Only
    one `active` plan per goal at a time (enforced in the service layer, not a DB constraint,
    since "supersede the previous active plan" is a two-row transition no single-row CHECK can
    express — mirrors how `ProjectNote.status` transitions are service-enforced, not
    constraint-enforced, per `app/project_memory.py`).
  - `mainai_tasks`: one node in the task graph. `status` is richer than `mainai_jobs.status`
    for the same reason as `mainai_goals` (adds `pending`/`ready`/`waiting_external`/
    `waiting_ci`/`blocked`/`retryable_failed`, values a bare job-status enum has no room for).
  - `mainai_task_dependencies`: edges of the task graph (`task_id` depends on
    `depends_on_task_id`). A DB CHECK denies a literal self-edge; general cycle detection
    still needs an application-level DFS at plan-creation time (see the planner service) since
    no single-row CHECK can express a multi-row cycle.
  - `mainai_task_events`: append-only execution-event history for a task, same DB-enforced
    immutability pattern as `mainai_job_events` (migration 0027) — a BEFORE UPDATE/DELETE
    trigger denies UPDATE unconditionally and DELETE unless the owner-erasure GUC is set. This
    migration folds that correction in from the start (0026/0027 needed two rounds only
    because of real review timing, not because the two-step split is required).
  - `mainai_checkpoints`: durable resume state, also append-only (same trigger pattern) —
    resume always reads the latest checkpoint row for a goal, never mutates an old one.
  - `engineering_lessons`: the safety-memory foundation. Deliberately NOT RLS-protected —
    like `project_notes` (see `app/models/project_memory.py`'s own docstring) this is
    founder-wide project/system knowledge, not per-owner personal data, in a founder-only
    system. Follows `ProjectNote`'s exact provenance shape (`source_type`/`source_ref`,
    mandatory, never a bare unsourced claim) plus the fields the founder's V0.1 spec requires
    (`root_cause`/`severity`/`fix`/`regression_test`/`general_rule`/`applies_to`/`confidence`/
    `resolved_in`). `status` reuses `ProjectNote`'s open/resolved/superseded shape
    (renamed active/historical/superseded to match `ActiveTruthStatus`'s vocabulary, since a
    lesson's truthfulness — not its "am I done with this note" state — is what matters here).
    Deliberately its OWN table, not a new `KnowledgeClaim` row: a lesson's fields don't fit a
    single `claim_text` column, matching the same reasoning `mainai_job_proposals` used to stay
    out of `knowledge_claims` (nothing here auto-promotes into founder-approved truth either).

Composite owner FKs (`UNIQUE(id, owner_id)` on each parent + `FOREIGN KEY (child_id, owner_id)
REFERENCES parent (id, owner_id)` on each child) are applied from the start on every
owner-scoped parent/child pair, closing the exact cross-owner-injection class of bug migration
0027 had to retrofit onto `mainai_jobs`.

`mainai_app`'s exact grants (SELECT/INSERT on the owner-scoped tables, EXECUTE on the new
erasure function) are applied and verified by `app/rls.py`'s
`apply_mainai_execution_privileges()`, called from `app/main.py` startup — same split as
`mainai_jobs`: no `GRANT ... TO mainai_app` here, so this migration applies cleanly on a bare
database where that role doesn't exist yet.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-10
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- mainai_goals
    op.execute("""
        CREATE TABLE mainai_goals (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title varchar(200) NOT NULL,
            original_instruction text NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            created_at timestamp without time zone NOT NULL,
            started_at timestamp without time zone,
            completed_at timestamp without time zone,
            current_plan_version integer NOT NULL DEFAULT 0,
            risk_level varchar(16) NOT NULL DEFAULT 'low',
            approval_policy varchar(32) NOT NULL DEFAULT 'standard_repo_work',
            final_outcome text,
            created_by varchar(64) NOT NULL,
            CONSTRAINT uq_mainai_goals_id_owner_id UNIQUE (id, owner_id),
            CONSTRAINT ck_mainai_goals_status CHECK (
                status IN ('pending', 'planning', 'running', 'waiting', 'blocked', 'failed', 'completed', 'cancelled')
            ),
            CONSTRAINT ck_mainai_goals_risk_level CHECK (risk_level IN ('low', 'medium', 'high')),
            CONSTRAINT ck_mainai_goals_current_plan_version_nonneg CHECK (current_plan_version >= 0),
            CONSTRAINT ck_mainai_goals_completed_at_matches_terminal_status CHECK (
                (completed_at IS NULL) = (status NOT IN ('completed', 'failed', 'cancelled'))
            )
        );
        CREATE INDEX ix_mainai_goals_owner_id ON mainai_goals USING btree (owner_id);
        CREATE INDEX ix_mainai_goals_status ON mainai_goals USING btree (status);

        ALTER TABLE mainai_goals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_goals FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_goals_isolation ON mainai_goals
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_plans
    op.execute("""
        CREATE TABLE mainai_plans (
            id uuid NOT NULL PRIMARY KEY,
            goal_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            version integer NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'draft',
            rationale text NOT NULL,
            created_at timestamp without time zone NOT NULL,
            created_by varchar(64) NOT NULL,
            CONSTRAINT uq_mainai_plans_id_owner_id UNIQUE (id, owner_id),
            CONSTRAINT uq_mainai_plans_goal_version UNIQUE (goal_id, version),
            CONSTRAINT ck_mainai_plans_status CHECK (status IN ('draft', 'active', 'superseded')),
            CONSTRAINT ck_mainai_plans_version_positive CHECK (version >= 1),
            CONSTRAINT fk_mainai_plans_goal_owner
                FOREIGN KEY (goal_id, owner_id) REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_mainai_plans_goal_id ON mainai_plans USING btree (goal_id);
        CREATE INDEX ix_mainai_plans_owner_id ON mainai_plans USING btree (owner_id);

        ALTER TABLE mainai_plans ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_plans FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_plans_isolation ON mainai_plans
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_tasks
    op.execute("""
        CREATE TABLE mainai_tasks (
            id uuid NOT NULL PRIMARY KEY,
            goal_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            description text NOT NULL,
            task_type varchar(32) NOT NULL,
            status varchar(24) NOT NULL DEFAULT 'pending',
            priority integer NOT NULL DEFAULT 0,
            risk_level varchar(16) NOT NULL DEFAULT 'low',
            approval_required boolean NOT NULL DEFAULT false,
            verification_plan jsonb NOT NULL DEFAULT '[]'::jsonb,
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            blocker_reason text,
            mainai_job_id uuid REFERENCES mainai_jobs(id) ON DELETE SET NULL,
            created_at timestamp without time zone NOT NULL,
            started_at timestamp without time zone,
            completed_at timestamp without time zone,
            CONSTRAINT uq_mainai_tasks_id_owner_id UNIQUE (id, owner_id),
            CONSTRAINT ck_mainai_tasks_status CHECK (
                status IN (
                    'pending', 'ready', 'running', 'waiting_external', 'waiting_ci',
                    'blocked', 'retryable_failed', 'failed', 'completed', 'cancelled'
                )
            ),
            CONSTRAINT ck_mainai_tasks_risk_level CHECK (risk_level IN ('low', 'medium', 'high')),
            CONSTRAINT ck_mainai_tasks_attempts_nonneg CHECK (attempts >= 0),
            CONSTRAINT ck_mainai_tasks_max_attempts_nonneg CHECK (max_attempts >= 0),
            CONSTRAINT ck_mainai_tasks_attempts_within_budget CHECK (attempts <= max_attempts),
            CONSTRAINT ck_mainai_tasks_completed_at_matches_terminal_status CHECK (
                (completed_at IS NULL) = (status NOT IN ('completed', 'failed', 'cancelled'))
            ),
            CONSTRAINT fk_mainai_tasks_goal_owner
                FOREIGN KEY (goal_id, owner_id) REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_mainai_tasks_plan_owner
                FOREIGN KEY (plan_id, owner_id) REFERENCES mainai_plans (id, owner_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_mainai_tasks_goal_id ON mainai_tasks USING btree (goal_id);
        CREATE INDEX ix_mainai_tasks_plan_id ON mainai_tasks USING btree (plan_id);
        CREATE INDEX ix_mainai_tasks_owner_id ON mainai_tasks USING btree (owner_id);
        CREATE INDEX ix_mainai_tasks_status ON mainai_tasks USING btree (status);
        CREATE INDEX ix_mainai_tasks_mainai_job_id ON mainai_tasks USING btree (mainai_job_id);

        ALTER TABLE mainai_tasks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_tasks FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_tasks_isolation ON mainai_tasks
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_task_dependencies
    op.execute("""
        CREATE TABLE mainai_task_dependencies (
            task_id uuid NOT NULL,
            depends_on_task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamp without time zone NOT NULL,
            PRIMARY KEY (task_id, depends_on_task_id),
            CONSTRAINT ck_mainai_task_dependencies_no_self_edge CHECK (task_id <> depends_on_task_id),
            CONSTRAINT fk_mainai_task_dependencies_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_mainai_task_dependencies_depends_on_owner
                FOREIGN KEY (depends_on_task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_mainai_task_dependencies_depends_on_task_id
            ON mainai_task_dependencies USING btree (depends_on_task_id);
        CREATE INDEX ix_mainai_task_dependencies_owner_id ON mainai_task_dependencies USING btree (owner_id);

        ALTER TABLE mainai_task_dependencies ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_task_dependencies FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_task_dependencies_isolation ON mainai_task_dependencies
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_task_events (append-only)
    op.execute("""
        CREATE TABLE mainai_task_events (
            id uuid NOT NULL PRIMARY KEY,
            task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT ck_mainai_task_events_event_type CHECK (
                event_type IN (
                    'created', 'ready', 'dispatched', 'progress_updated', 'verification_started',
                    'verification_passed', 'verification_failed', 'blocked', 'replanned',
                    'retry_scheduled', 'approval_required', 'approval_granted', 'completed',
                    'failed', 'cancelled'
                )
            ),
            CONSTRAINT fk_mainai_task_events_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_mainai_task_events_task_id ON mainai_task_events USING btree (task_id);
        CREATE INDEX ix_mainai_task_events_owner_id ON mainai_task_events USING btree (owner_id);
        CREATE INDEX ix_mainai_task_events_created_at ON mainai_task_events USING btree (created_at);

        ALTER TABLE mainai_task_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_task_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_task_events_isolation ON mainai_task_events
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION mainai_task_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.mainai_execution_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'mainai_task_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'mainai_task_events is append-only: UPDATE is never permitted.';
        END;
        $$;

        REVOKE ALL ON FUNCTION mainai_task_events_deny_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_mainai_task_events_deny_mutation
            BEFORE UPDATE OR DELETE ON mainai_task_events
            FOR EACH ROW EXECUTE FUNCTION mainai_task_events_deny_mutation();
    """)

    # ---------------------------------------------------------------- mainai_checkpoints (append-only)
    op.execute("""
        CREATE TABLE mainai_checkpoints (
            id uuid NOT NULL PRIMARY KEY,
            goal_id uuid NOT NULL,
            task_id uuid,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_version integer NOT NULL,
            executor_state jsonb NOT NULL DEFAULT '{}'::jsonb,
            test_status varchar(32),
            ci_status varchar(32),
            blocker text,
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT fk_mainai_checkpoints_goal_owner
                FOREIGN KEY (goal_id, owner_id) REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_mainai_checkpoints_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE SET NULL
        );
        CREATE INDEX ix_mainai_checkpoints_goal_id_created_at
            ON mainai_checkpoints USING btree (goal_id, created_at);
        CREATE INDEX ix_mainai_checkpoints_task_id ON mainai_checkpoints USING btree (task_id);
        CREATE INDEX ix_mainai_checkpoints_owner_id ON mainai_checkpoints USING btree (owner_id);

        ALTER TABLE mainai_checkpoints ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_checkpoints FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_checkpoints_isolation ON mainai_checkpoints
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION mainai_checkpoints_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.mainai_execution_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'mainai_checkpoints is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'mainai_checkpoints is append-only: UPDATE is never permitted.';
        END;
        $$;

        REVOKE ALL ON FUNCTION mainai_checkpoints_deny_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_mainai_checkpoints_deny_mutation
            BEFORE UPDATE OR DELETE ON mainai_checkpoints
            FOR EACH ROW EXECUTE FUNCTION mainai_checkpoints_deny_mutation();
    """)

    # ---------------------------------------------------------------- owner erasure (goals/plans cascade via FK;
    # events/checkpoints need the same erasure-flag bypass mainai_job_events uses, since a plain
    # DELETE from an owner-erasure flow would otherwise be rejected by the triggers above)
    op.execute("""
        CREATE FUNCTION erase_own_mainai_execution_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_mainai_execution_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.mainai_execution_erasure_in_progress', 'on', true);
            DELETE FROM public.mainai_checkpoints WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_events WHERE owner_id = v_owner_id;
            -- mainai_task_dependencies/mainai_tasks/mainai_plans/mainai_goals are NOT
            -- append-only; they're deleted directly by the caller (mirroring how
            -- erase_own_mainai_job_children() leaves mainai_jobs itself to the caller) via the
            -- goal_id -> ... -> ON DELETE CASCADE chain, so a single `DELETE FROM mainai_goals
            -- WHERE owner_id = ...` after this call removes the rest.
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)

    # ---------------------------------------------------------------- engineering_lessons
    # Founder-wide, NOT owner-scoped, NOT RLS-protected — same reasoning as `project_notes`
    # (see app/models/project_memory.py's ProjectNote docstring): this is project/system
    # knowledge, not per-owner personal data, in a founder-only system.
    op.execute("""
        CREATE TABLE engineering_lessons (
            id uuid NOT NULL PRIMARY KEY,
            status varchar(16) NOT NULL DEFAULT 'active',
            problem text NOT NULL,
            root_cause text NOT NULL,
            affected_component varchar(128) NOT NULL,
            severity varchar(16) NOT NULL,
            evidence text NOT NULL,
            fix text NOT NULL,
            regression_test varchar(256),
            general_rule text NOT NULL,
            applies_to jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_type varchar(32) NOT NULL,
            source_ref varchar(256) NOT NULL,
            first_seen_at timestamp without time zone NOT NULL,
            resolved_in varchar(256),
            confidence varchar(16) NOT NULL DEFAULT 'likely',
            created_by varchar(64) NOT NULL,
            created_at timestamp without time zone NOT NULL,
            superseded_by uuid REFERENCES engineering_lessons(id) ON DELETE SET NULL,
            CONSTRAINT ck_engineering_lessons_status CHECK (status IN ('active', 'historical', 'superseded')),
            CONSTRAINT ck_engineering_lessons_severity CHECK (severity IN ('low', 'medium', 'high', 'critical')),
            CONSTRAINT ck_engineering_lessons_confidence CHECK (confidence IN ('certain', 'likely', 'uncertain')),
            CONSTRAINT ck_engineering_lessons_superseded_requires_link CHECK (
                (status = 'superseded') = (superseded_by IS NOT NULL)
            )
        );
        CREATE INDEX ix_engineering_lessons_status ON engineering_lessons USING btree (status);
        CREATE INDEX ix_engineering_lessons_affected_component ON engineering_lessons USING btree (affected_component);
        CREATE INDEX ix_engineering_lessons_created_at ON engineering_lessons USING btree (created_at);
        -- GIN index so applies_to tag lookups (planner/verifier relevant-lesson queries) are a
        -- real index scan, not a sequential scan over every lesson ever recorded.
        CREATE INDEX ix_engineering_lessons_applies_to ON engineering_lessons USING gin (applies_to);
    """)

    # Deliberately NOT here: no GRANT/REVOKE naming `mainai_app` — see this file's own
    # docstring. Grants are applied and verified by app/rls.py's
    # apply_mainai_execution_privileges(), called from app/main.py startup after both Alembic
    # and scripts/ensure_app_role.py have already run.


def downgrade() -> None:
    op.execute("DROP TABLE engineering_lessons;")

    op.execute("DROP FUNCTION erase_own_mainai_execution_children();")

    op.execute("""
        DROP TRIGGER trg_mainai_checkpoints_deny_mutation ON mainai_checkpoints;
        DROP FUNCTION mainai_checkpoints_deny_mutation();
    """)
    op.execute("DROP TABLE mainai_checkpoints;")

    op.execute("""
        DROP TRIGGER trg_mainai_task_events_deny_mutation ON mainai_task_events;
        DROP FUNCTION mainai_task_events_deny_mutation();
    """)
    op.execute("DROP TABLE mainai_task_events;")

    op.execute("DROP TABLE mainai_task_dependencies;")
    op.execute("DROP TABLE mainai_tasks;")
    op.execute("DROP TABLE mainai_plans;")
    op.execute("DROP TABLE mainai_goals;")
