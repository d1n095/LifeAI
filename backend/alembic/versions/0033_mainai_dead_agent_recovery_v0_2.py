"""MainAI V0.2 — Dead Agent Recovery: durable worktree ownership, recovery inspection/
classification, and append-only recovery event history.

This is a NEW table family sitting ABOVE the existing V0.1 execution loop (migration 0032)
and the existing `mainai_jobs` lease/fencing runtime (migrations 0026-0029) — exactly the
same "reuse, don't reimplement" relationship 0032 has to 0026-0029. This migration adds ZERO
new lease/fencing/heartbeat columns anywhere: whether an executor is still allowed to write
authoritative state continues to be governed entirely by `mainai_jobs.lease_generation` /
`JobLeaseLostError` (see `app/jobs/mainai_job_lease.py`, `app/jobs/service.py`) — recovery
only ever reads that state and dispatches a NEW job (via the existing `dispatch_ready_task()`)
for a takeover attempt, it never grants write authority itself.

Two facts drove this schema, both verified against the real V0.1 code and the real deployment
before writing a single column (see `docs/MAINAI_DEAD_AGENT_RECOVERY_V0_2.md`'s own opening
section for the full writeup):

1. V0.1 has NO worktree/local-clone isolation and NO local `git commit` step — `repo_edit`
   writes files directly onto the single shared on-disk backend checkout the worker process
   itself runs from, and all real git history operations happen exclusively through the
   GitHub Git Data API (branch/commit/push are one remote operation, not a local-commit-then-
   push sequence). This migration's `mainai_task_worktrees` table is what INTRODUCES minimal
   per-task/per-attempt filesystem isolation as a small prerequisite this recovery system
   needs — it does not merely track a pre-existing concept.

2. The backend/worker container's filesystem is NOT persistent (only the `lifeai_uploads`
   named volume is, per `docker-compose.vps.yml`) — a container replacement wipes any local
   file state unconditionally; only a same-container process-level restart preserves it. A
   worktree row therefore can NEVER be trusted by path/task_id alone (an unrelated, stale
   directory could coincidentally exist at a reused path after a redeploy). Trust requires
   proving the on-disk `marker_token` inside the worktree still matches this table's own
   `marker_token` column — see `app/mainai_execution/recovery_inspector.py` for the read-back
   check. A missing or mismatched marker is treated as "no local work to trust", never
   silently assumed either way.

Three tables:

  - `mainai_task_worktrees`: one row per (task, job/attempt) filesystem worktree this system
    creates. Mutable (`status` transitions active -> released/abandoned as a worktree's
    lifecycle progresses) — not append-only, matching `mainai_tasks` itself, not
    `mainai_task_events`. `UNIQUE(job_id)`: a `mainai_jobs` row is itself already a single
    dispatch attempt (`dispatch_ready_task()` mints a new job per ready-dispatch, see
    executor.py), so at most one worktree ever belongs to one job. Per the founder's explicit
    ownership-metadata list: `repo`/`base_sha`/`branch` are NOT NULL (a worktree is only ever
    created from a verified base SHA, with a task-scoped branch — never left implicit), a
    CHECK denies `branch` ever being a protected/mainline name, `current_commit` tracks the
    latest local commit as `worktree.py` advances it, and `recovery_state` (separate from the
    worktree's own `status` lifecycle) tracks whether a recovery pass has verified this
    worktree's ownership before any takeover is allowed to reuse it — starts `unclaimed`,
    only ever becomes `claimed_for_takeover` after `verify_worktree_ownership()` succeeds.

  - `mainai_recovery_records`: one row per dead/stalled job a recovery pass has looked at.
    `classification` is the founder's own A-I vocabulary (documented in the CHECK below and
    in the design doc), nullable until the inspector has actually run. `UNIQUE(job_id)`: a
    job only ever dies once; a takeover creates a brand NEW job_id for the new attempt via
    the existing `dispatch_ready_task()`, which gets its own recovery record only if it also
    later dies.

  - `mainai_recovery_events`: append-only history of a recovery record's lifecycle, same
    DB-trigger-enforced immutability pattern `mainai_task_events`/`mainai_checkpoints`
    established in migration 0032 (BEFORE UPDATE/DELETE trigger denies UPDATE
    unconditionally, denies DELETE unless `app.mainai_execution_erasure_in_progress` is set —
    the SAME GUC 0032 already uses, not a new one, since this is the same execution/erasure
    domain). `erase_own_mainai_execution_children()` (0032) is extended here via
    `CREATE OR REPLACE FUNCTION` to also clear these three new tables for an owner-erasure
    flow, in the same single transaction the caller already runs that function inside.

Composite owner FKs (`FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id)`)
are applied on all three tables from the start, matching 0032's own cross-owner-injection
defense.

`mainai_app`'s exact grants (SELECT/INSERT/UPDATE on the owner-scoped mutable table, SELECT/
INSERT on the two append-only-by-convention-or-trigger tables) are applied and verified by
`app/rls.py`'s `apply_mainai_execution_privileges()`, extended to cover these three tables —
same split 0032 itself uses: no `GRANT ... TO mainai_app` here, so this migration applies
cleanly on a bare database where that role doesn't exist yet.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-11
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- mainai_task_worktrees
    op.execute("""
        CREATE TABLE mainai_task_worktrees (
            id uuid NOT NULL PRIMARY KEY,
            task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id uuid NOT NULL UNIQUE REFERENCES mainai_jobs(id) ON DELETE CASCADE,
            lease_generation integer NOT NULL,
            executor_id varchar(128) NOT NULL,
            repo varchar(255) NOT NULL,
            base_sha varchar(64) NOT NULL,
            branch varchar(255) NOT NULL,
            path text NOT NULL,
            marker_token varchar(64) NOT NULL,
            current_commit varchar(64),
            recovery_state varchar(32) NOT NULL DEFAULT 'unclaimed',
            status varchar(16) NOT NULL DEFAULT 'active',
            created_at timestamp without time zone NOT NULL,
            released_at timestamp without time zone,
            CONSTRAINT fk_mainai_task_worktrees_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_mainai_task_worktrees_status CHECK (
                status IN ('active', 'released', 'abandoned')
            ),
            CONSTRAINT ck_mainai_task_worktrees_released_at_matches_status CHECK (
                (released_at IS NULL) = (status = 'active')
            ),
            CONSTRAINT ck_mainai_task_worktrees_recovery_state CHECK (
                recovery_state IN ('unclaimed', 'ownership_verified', 'ownership_rejected', 'claimed_for_takeover')
            ),
            -- Never mainline/the branch this whole recovery system's own PRs land on -- a task
            -- worktree's working branch must always be the deterministic per-task name
            -- (`claude/mainai-task-<task_id>`), enforced here as well as in worktree.py itself
            -- (defense in depth: a bug in the Python guard must not be the only thing standing
            -- between a task attempt and writing directly to the branch other work depends on).
            CONSTRAINT ck_mainai_task_worktrees_branch_not_protected CHECK (
                branch NOT IN ('main', 'master', 'claude/det-kommer-mer-879lcm')
            )
        );
        CREATE INDEX ix_mainai_task_worktrees_task_id ON mainai_task_worktrees USING btree (task_id);
        CREATE INDEX ix_mainai_task_worktrees_owner_id ON mainai_task_worktrees USING btree (owner_id);

        ALTER TABLE mainai_task_worktrees ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_task_worktrees FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_task_worktrees_isolation ON mainai_task_worktrees
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_recovery_records
    op.execute("""
        CREATE TABLE mainai_recovery_records (
            id uuid NOT NULL PRIMARY KEY,
            task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id uuid NOT NULL UNIQUE REFERENCES mainai_jobs(id) ON DELETE CASCADE,
            detected_at timestamp without time zone NOT NULL,
            classification varchar(32),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            salvage_action varchar(128),
            takeover_executor varchar(128),
            takeover_job_id uuid REFERENCES mainai_jobs(id) ON DELETE SET NULL,
            status varchar(24) NOT NULL DEFAULT 'detected',
            blocker text,
            manual_review_required boolean NOT NULL DEFAULT false,
            completed_at timestamp without time zone,
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT fk_mainai_recovery_records_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_mainai_recovery_records_status CHECK (
                status IN (
                    'detected', 'inspecting', 'inspected', 'classified', 'salvaging', 'salvaged',
                    'taking_over', 'taken_over', 'blocked', 'completed'
                )
            ),
            CONSTRAINT ck_mainai_recovery_records_classification CHECK (
                classification IS NULL OR classification IN (
                    'NOTHING_DONE', 'CHECKPOINTED_WORK', 'LOCAL_UNCOMMITTED_WORK',
                    'LOCAL_COMMITTED_NOT_PUSHED', 'PUSHED_NO_PR', 'PR_EXISTS', 'VERIFIED_WORK',
                    'CONFLICTED_STATE', 'UNSAFE_TO_AUTO_RECOVER'
                )
            ),
            CONSTRAINT ck_mainai_recovery_records_completed_at_matches_status CHECK (
                (completed_at IS NULL) = (status <> 'completed')
            ),
            CONSTRAINT ck_mainai_recovery_records_blocked_requires_reason CHECK (
                (status <> 'blocked') OR (blocker IS NOT NULL)
            )
        );
        CREATE INDEX ix_mainai_recovery_records_task_id ON mainai_recovery_records USING btree (task_id);
        CREATE INDEX ix_mainai_recovery_records_owner_id ON mainai_recovery_records USING btree (owner_id);
        CREATE INDEX ix_mainai_recovery_records_status ON mainai_recovery_records USING btree (status);

        ALTER TABLE mainai_recovery_records ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_recovery_records FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_recovery_records_isolation ON mainai_recovery_records
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # ---------------------------------------------------------------- mainai_recovery_events (append-only)
    op.execute("""
        CREATE TABLE mainai_recovery_events (
            id uuid NOT NULL PRIMARY KEY,
            recovery_record_id uuid NOT NULL REFERENCES mainai_recovery_records(id) ON DELETE CASCADE,
            task_id uuid NOT NULL,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT fk_mainai_recovery_events_task_owner
                FOREIGN KEY (task_id, owner_id) REFERENCES mainai_tasks (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_mainai_recovery_events_event_type CHECK (
                event_type IN (
                    'stalled_detected', 'dead_detected', 'recovery_started', 'recovery_inspected',
                    'recovery_classified', 'salvage_started', 'salvage_completed',
                    'takeover_started', 'takeover_completed', 'recovery_blocked',
                    'manual_review_required'
                )
            )
        );
        CREATE INDEX ix_mainai_recovery_events_recovery_record_id
            ON mainai_recovery_events USING btree (recovery_record_id);
        CREATE INDEX ix_mainai_recovery_events_task_id ON mainai_recovery_events USING btree (task_id);
        CREATE INDEX ix_mainai_recovery_events_owner_id ON mainai_recovery_events USING btree (owner_id);
        CREATE INDEX ix_mainai_recovery_events_created_at ON mainai_recovery_events USING btree (created_at);

        ALTER TABLE mainai_recovery_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_recovery_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_recovery_events_isolation ON mainai_recovery_events
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION mainai_recovery_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.mainai_execution_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'mainai_recovery_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'mainai_recovery_events is append-only: UPDATE is never permitted.';
        END;
        $$;

        REVOKE ALL ON FUNCTION mainai_recovery_events_deny_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_mainai_recovery_events_deny_mutation
            BEFORE UPDATE OR DELETE ON mainai_recovery_events
            FOR EACH ROW EXECUTE FUNCTION mainai_recovery_events_deny_mutation();
    """)

    # ---------------------------------------------------------------- owner erasure: extend 0032's function
    # CREATE OR REPLACE, not a new function -- callers (app/rag/account_erasure.py) already call
    # `erase_own_mainai_execution_children()` by this exact name; this is the SAME single-transaction
    # extension point 0032 itself documents (goals/plans/tasks/dependencies still handled by the
    # caller's own `DELETE FROM mainai_goals WHERE owner_id = ...` cascade, unchanged by this migration).
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


def downgrade() -> None:
    # Restore 0032's original function body exactly (removing the three new DELETEs) before
    # dropping the tables those DELETEs reference -- CREATE OR REPLACE first, same reasoning
    # 0032's own downgrade uses when it hands a table back to a clean pre-migration state.
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
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_own_mainai_execution_children() FROM PUBLIC;
    """)

    op.execute("DROP TRIGGER trg_mainai_recovery_events_deny_mutation ON mainai_recovery_events;")
    op.execute("DROP FUNCTION mainai_recovery_events_deny_mutation();")
    op.execute("DROP TABLE mainai_recovery_events;")
    op.execute("DROP TABLE mainai_recovery_records;")
    op.execute("DROP TABLE mainai_task_worktrees;")
