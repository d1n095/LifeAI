"""MainAI V0.3 — Long-Running Orchestration: durable external-wait tracking (CI-wait first),
retry-with-backoff scheduling, and new append-only event vocabulary for wait/cancel/auto-
recovery/lesson-conflict lifecycles.

Architecture facts verified against the real V0.1/V0.2 code before writing a single column
(see docs/MAINAI_LONG_RUNNING_ORCHESTRATION_V0_3.md's opening section for the full writeup):

1. `mainai_tasks.status` (migration 0032) already contains `waiting_external`/`waiting_ci` as
   CHECK-allowed values — added in V0.1, never dispatched into by any code path since. This
   migration does NOT touch that CHECK; it only adds what's needed to make those two states
   actually reachable: a durable record of WHAT a task is waiting for, evaluated by WHOM, and
   WHEN to poll again.
2. `mainai_checkpoints` already has `ci_status`/`test_status` columns (migration 0032) that no
   code path writes — anticipated exactly this work. Left untouched here; the new
   `mainai_task_waits` table is the durable source of truth for wait state (a checkpoint
   remains a resume snapshot, not a live poll schedule).
3. `mainai_jobs.cancel_requested`/`cancel_acknowledged` and `app/jobs/service.py`'s
   `request_cancel()`/`mark_cancelled()` already exist and are fully reused, unchanged — no
   new job-level cancellation column anywhere in this migration.
4. `mainai_tasks.attempts`/`max_attempts` already exist; `app/jobs/retry.py`'s
   `compute_backoff_seconds()` already exists and is reused for the actual backoff formula —
   the only new column is `next_retry_at`, a due-time the worker's bounded retry-scan can
   index on, mirroring `mainai_jobs.lease_expires_at`'s own indexed-due-time role.
5. `mainai_plans`/`create_plan()` (app/mainai_execution/planner.py) already fully implement
   versioned replan-with-supersession (a replan creates NEW task rows with NEW task_ids) — a
   prior task's `approval_granted` MainAITaskEvent is scoped to the OLD task_id and therefore
   never applies to the new one by construction. No new column is needed for approval
   escalation; V0.3 only adds the automatic TRIGGER point (application code, not schema).

One new table:

  - `mainai_task_waits`: one row per durable external-wait a task_execution job created.
    General-but-minimal (`source_type`/`resource_ref`/`condition` are all closed-vocabulary-
    or-JSON, not a github-specific schema) so a second wait source could be added later without
    a new table — but only `github_check_runs` is actually implemented in V0.3. `UNIQUE(job_id)`
    matches `mainai_task_worktrees`/`mainai_recovery_records`'s own precedent (migration 0033):
    one job attempt creates at most one wait. Mutable (`status` progresses pending ->
    satisfied/failed/timed_out/cancelled) — not append-only, matching `mainai_task_worktrees`,
    not `mainai_task_events`. `next_poll_at` is indexed for the worker's bounded, due-only scan
    (see app/worker.py's `_poll_mainai_task_waits`); `deadline_at` is NOT NULL — every wait has
    a real timeout, satisfying the founder's explicit "a task must never become falsely
    completed by waiting forever" requirement.

Extends `mainai_task_events.event_type`'s CHECK (same pattern migration 0035 already used for
`mainai_recovery_events.event_type`) with the wait/cancel/auto-recovery/lesson-conflict
vocabulary V0.3's new code paths record. Extends `engineering_lessons.status`'s CHECK with
`disputed`, the one new value V0.3's minimal lesson-conflict detection needs (a contradiction
pauses BOTH lessons at `disputed` rather than silently picking one — see
app/mainai_execution/lesson_conflicts.py).

Adds `mainai_tasks.next_retry_at` (nullable timestamp, indexed) for the retry-with-backoff
worker scan.

Composite owner FK (`FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id)`)
on `mainai_task_waits`, matching every prior migration's cross-owner-injection defense.

`mainai_app`'s grants (SELECT/INSERT/UPDATE, mutable table) are applied and verified by
`app/rls.py`'s `apply_mainai_execution_privileges()`, extended to cover this new table — no
`GRANT ... TO mainai_app` here, so this migration applies cleanly on a bare database where that
role doesn't exist yet, same as every prior mainai_execution migration.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-11
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_OLD_TASK_EVENT_TYPES = (
    "'created', 'ready', 'dispatched', 'progress_updated', 'verification_started', "
    "'verification_passed', 'verification_failed', 'blocked', 'replanned', "
    "'retry_scheduled', 'approval_required', 'approval_granted', 'completed', "
    "'failed', 'cancelled'"
)
_NEW_TASK_EVENT_TYPES = _OLD_TASK_EVENT_TYPES + (
    ", 'wait_started', 'wait_satisfied', 'wait_failed', 'wait_timed_out', "
    "'cancel_requested', 'cancelling', 'auto_recovery_triggered', 'lesson_conflict_detected'"
)

_OLD_LESSON_STATUSES = "'active', 'historical', 'superseded'"
_NEW_LESSON_STATUSES = _OLD_LESSON_STATUSES + ", 'disputed'"


def upgrade() -> None:
    # ---------------------------------------------------------------- mainai_task_waits
    op.execute("""
        CREATE TABLE mainai_task_waits (
            id uuid NOT NULL PRIMARY KEY,
            task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id uuid NOT NULL UNIQUE REFERENCES mainai_jobs(id) ON DELETE CASCADE,
            source_type varchar(32) NOT NULL,
            resource_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
            condition jsonb NOT NULL DEFAULT '{}'::jsonb,
            status varchar(16) NOT NULL DEFAULT 'pending',
            poll_count integer NOT NULL DEFAULT 0,
            last_polled_at timestamp without time zone,
            next_poll_at timestamp without time zone NOT NULL,
            backoff_seconds integer NOT NULL DEFAULT 30,
            deadline_at timestamp without time zone NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp without time zone NOT NULL,
            resolved_at timestamp without time zone,
            CONSTRAINT fk_mainai_task_waits_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_mainai_task_waits_source_type CHECK (
                source_type IN ('github_check_runs')
            ),
            CONSTRAINT ck_mainai_task_waits_status CHECK (
                status IN ('pending', 'satisfied', 'failed', 'timed_out', 'cancelled')
            ),
            CONSTRAINT ck_mainai_task_waits_poll_count_nonneg CHECK (poll_count >= 0),
            CONSTRAINT ck_mainai_task_waits_backoff_seconds_positive CHECK (backoff_seconds > 0),
            CONSTRAINT ck_mainai_task_waits_resolved_at_matches_status CHECK (
                (resolved_at IS NULL) = (status = 'pending')
            )
        );
        CREATE INDEX ix_mainai_task_waits_task_id ON mainai_task_waits USING btree (task_id);
        CREATE INDEX ix_mainai_task_waits_owner_id ON mainai_task_waits USING btree (owner_id);
        CREATE INDEX ix_mainai_task_waits_status ON mainai_task_waits USING btree (status);
        CREATE INDEX ix_mainai_task_waits_next_poll_at ON mainai_task_waits USING btree (next_poll_at);

        ALTER TABLE mainai_task_waits ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_task_waits FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_task_waits_isolation ON mainai_task_waits
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_tasks.next_retry_at
    op.execute("""
        ALTER TABLE mainai_tasks ADD COLUMN next_retry_at timestamp without time zone;
        CREATE INDEX ix_mainai_tasks_next_retry_at ON mainai_tasks USING btree (next_retry_at);
    """)

    # ---------------------------------------------------------------- mainai_task_events.event_type
    op.execute(f"""
        ALTER TABLE mainai_task_events DROP CONSTRAINT ck_mainai_task_events_event_type;
        ALTER TABLE mainai_task_events ADD CONSTRAINT ck_mainai_task_events_event_type CHECK (
            event_type IN ({_NEW_TASK_EVENT_TYPES})
        );
    """)

    # ---------------------------------------------------------------- engineering_lessons.status
    op.execute(f"""
        ALTER TABLE engineering_lessons DROP CONSTRAINT ck_engineering_lessons_status;
        ALTER TABLE engineering_lessons ADD CONSTRAINT ck_engineering_lessons_status CHECK (
            status IN ({_NEW_LESSON_STATUSES})
        );
    """)

    # ---------------------------------------------------------------- owner erasure: extend 0032/0033's function
    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
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
            DELETE FROM public.mainai_recovery_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_records WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_worktrees WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_waits WHERE owner_id = v_owner_id;
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION erase_own_mainai_execution_children() RETURNS void
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
            DELETE FROM public.mainai_recovery_events WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_recovery_records WHERE owner_id = v_owner_id;
            DELETE FROM public.mainai_task_worktrees WHERE owner_id = v_owner_id;
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)

    op.execute(f"""
        ALTER TABLE engineering_lessons DROP CONSTRAINT ck_engineering_lessons_status;
        ALTER TABLE engineering_lessons ADD CONSTRAINT ck_engineering_lessons_status CHECK (
            status IN ({_OLD_LESSON_STATUSES})
        );
    """)

    op.execute(f"""
        ALTER TABLE mainai_task_events DROP CONSTRAINT ck_mainai_task_events_event_type;
        ALTER TABLE mainai_task_events ADD CONSTRAINT ck_mainai_task_events_event_type CHECK (
            event_type IN ({_OLD_TASK_EVENT_TYPES})
        );
    """)

    op.execute("""
        DROP INDEX ix_mainai_tasks_next_retry_at;
        ALTER TABLE mainai_tasks DROP COLUMN next_retry_at;
    """)

    op.execute("DROP TABLE mainai_task_waits;")
