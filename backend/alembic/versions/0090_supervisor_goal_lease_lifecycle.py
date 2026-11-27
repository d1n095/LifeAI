"""Supervisor goal-lease lifecycle -- bounded live rows, durable history (F4).

Measured in the isolated installation on 2026-11-27: `supervisor_goal_leases` grew by one
`released` row per Supervisor tick (46+ rows for a single goal in a short run). Nothing was
wrong with the lease itself -- the partial unique index only constrains ACTIVE rows, so every
tick inserted a fresh row and marked it released, and nothing ever cleaned up.

Two properties have to hold at the same time, which is why this is a lifecycle change rather
than a delete:

  * AT MOST ONE ROW PER GOAL in the live table. `claim_supervisor_goal_lease()` now recycles
    the goal's own released row (same row, generation bumped, `released_at` cleared) instead
    of inserting a new one; the expired-active takeover path keeps mutating in place exactly
    as before. Growth becomes O(goals), not O(ticks).
  * NO LOST AUDIT HISTORY. Recyccling a row would erase which worker held the lease, at which
    generation, and when it was released -- exactly what the accumulated rows accidentally
    provided. `supervisor_goal_lease_events` is the append-only replacement: one row per
    claim/takeover/release, written by the same function that changes the lease, in the same
    transaction. Renewals are deliberately NOT recorded (they change no ownership and would
    reintroduce per-tick growth); `last_heartbeat_at` on the live row stays their only trace.

Backfill: every existing lease row becomes its `claimed` event (and a `released` event when it
is already released) before the redundant released rows are removed. The live table therefore
keeps exactly one row per goal (the active one, if any), and the full pre-existing history
survives in the append-only table.

Same append-only enforcement shape as `mainai_job_events` (migration 0027): UPDATE is never
permitted; DELETE only inside an authorized owner erasure, gated by a session GUC that only
`erase_own_supervisor_goal_leases()` sets. That function is replaced here so account erasure
also removes the new history in the same transaction.
"""

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE supervisor_goal_lease_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            lease_id uuid NOT NULL,
            worker_id varchar(128) NOT NULL,
            lease_generation integer NOT NULL,
            event_type varchar(16) NOT NULL,
            acquired_at timestamp,
            released_at timestamp,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_supervisor_goal_lease_events_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_supervisor_goal_lease_events_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_supervisor_goal_lease_events_type
                CHECK (event_type IN ('claimed', 'taken_over', 'released')),
            CONSTRAINT ck_supervisor_goal_lease_events_generation CHECK (lease_generation >= 1)
        );
        CREATE INDEX ix_supervisor_goal_lease_events_owner_goal
            ON supervisor_goal_lease_events(owner_id, goal_id, created_at);
        CREATE INDEX ix_supervisor_goal_lease_events_lease
            ON supervisor_goal_lease_events(lease_id);

        ALTER TABLE supervisor_goal_lease_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE supervisor_goal_lease_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY supervisor_goal_lease_events_isolation ON supervisor_goal_lease_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """
    )

    # Backfill BEFORE any released row is removed: every existing row's own history becomes
    # its events. `taken_over` cannot be reconstructed for a row whose generation is above 1
    # (the old code bumped it in place), so those are recorded as `claimed` at their final
    # generation and the detail says so -- an honest gap in a reconstruction, not a invented
    # event.
    op.execute(
        """
        INSERT INTO supervisor_goal_lease_events
            (owner_id, goal_id, lease_id, worker_id, lease_generation, event_type, acquired_at, detail)
        SELECT owner_id, goal_id, id, worker_id, lease_generation, 'claimed', acquired_at,
               jsonb_build_object('backfilled_from', 'supervisor_goal_leases', 'reconstructed', true)
        FROM supervisor_goal_leases;

        INSERT INTO supervisor_goal_lease_events
            (owner_id, goal_id, lease_id, worker_id, lease_generation, event_type, released_at, detail)
        SELECT owner_id, goal_id, id, worker_id, lease_generation, 'released', released_at,
               jsonb_build_object('backfilled_from', 'supervisor_goal_leases', 'reconstructed', true)
        FROM supervisor_goal_leases
        WHERE status = 'released' AND released_at IS NOT NULL;
        """
    )

    # The history now lives in the append-only table. Keep the active row (there is at most one
    # per goal by the partial unique index) and drop the redundant released rows -- otherwise
    # the "at most one row per goal" property would only start holding for future ticks.
    op.execute("DELETE FROM supervisor_goal_leases WHERE status = 'released';")

    op.execute(
        """
        CREATE FUNCTION supervisor_goal_lease_events_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.supervisor_lease_erasure_in_progress', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'supervisor_goal_lease_events is append-only: DELETE is only permitted through an authorized owner erasure.';
            END IF;
            RAISE EXCEPTION 'supervisor_goal_lease_events is append-only: UPDATE is never permitted.';
        END;
        $$;

        REVOKE ALL ON FUNCTION supervisor_goal_lease_events_deny_mutation() FROM PUBLIC;

        CREATE TRIGGER trg_supervisor_goal_lease_events_deny_mutation
            BEFORE UPDATE OR DELETE ON supervisor_goal_lease_events
            FOR EACH ROW EXECUTE FUNCTION supervisor_goal_lease_events_deny_mutation();
        """
    )

    # Replaces 0059's function: account erasure must remove the new history too, and only the
    # erasure transaction may (the trigger above allows it only with this GUC set).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION erase_own_supervisor_goal_leases() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_supervisor_goal_leases requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.supervisor_lease_erasure_in_progress', 'on', true);
            DELETE FROM public.supervisor_goal_lease_events WHERE owner_id = v_owner_id;
            DELETE FROM public.supervisor_goal_leases WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_supervisor_goal_leases() FROM PUBLIC;
        """
    )


def downgrade() -> None:
    # Restore 0059's erasure function (the events table is about to disappear).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION erase_own_supervisor_goal_leases() RETURNS void
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
        """
    )
    op.execute(
        """
        DROP TRIGGER trg_supervisor_goal_lease_events_deny_mutation ON supervisor_goal_lease_events;
        DROP FUNCTION supervisor_goal_lease_events_deny_mutation();
        DROP TABLE supervisor_goal_lease_events;
        """
    )
